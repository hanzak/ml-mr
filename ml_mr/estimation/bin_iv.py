"""
Implementation of the Histogram or Quantile based deep IV model.

This model is similar to Hartford et al. ICML (2017) model, but it treats
the exposure using a classification approach based on binned from quantiles
or a histogram.

"""


import argparse
import itertools
import os
import pickle
import sys
from typing import Iterable, Iterator, List, Literal, Optional, Tuple

import matplotlib.pyplot as plt
import pandas as pd
import pytorch_lightning as pl
import torch
import torch.nn as nn
import torch.nn.functional as F
from pytorch_genotypes.dataset import (
    BACKENDS,
    GeneticDatasetBackend,
    PhenotypeGeneticDataset,
)
from torch.utils.data import DataLoader, Dataset, random_split
from torchmetrics import ConfusionMatrix

from ..logging import critical, info
from ..utils import MLP, read_data, temperature_scale
from .core import MREstimator


# Types and literal definitions.
BINNING_MODES = ["histogram", "qantiles"]
BinningMode = Literal["histogram", "quantiles"]


# Default values definitions.
# fmt: off
DEFAULTS = {
    "exposure_hidden": [128, 64],
    "outcome_hidden": [32, 16],
    "exposure_learning_rate": 5e-4,
    "outcome_learning_rate": 5e-4,
    "exposure_batch_size": 10_000,
    "outcome_batch_size": 10_000,
    "exposure_max_epochs": 1000,
    "outcome_max_epochs": 1000,
    "exposure_weight_decay": 1e-4,
    "outcome_weight_decay": 1e-4,
    "accelerator": "gpu" if (
        torch.cuda.is_available() and torch.cuda.device_count() > 0
    ) else "cpu",
    "validation_proportion": 0.2,
    "output_dir": "bin_iv_estimate",
}
# fmt: on


class Binning(object):
    def __init__(
        self,
        x: torch.Tensor,
        mode: BinningMode = "quantiles",
        n_bins: int = 20,
    ):
        x = x.to(torch.float32)
        self.n_bins = n_bins

        if mode == "histogram":
            self.bin_edges = self._bins_from_histogram(x)

        elif mode == "quantiles":
            self.bin_edges = self._bins_from_quantiles(x)

        else:
            raise ValueError(
                f"Unknown mode '{mode}'. Use one of {BINNING_MODES}"
            )

    def _bins_from_histogram(self, x: torch.Tensor) -> torch.Tensor:
        self._hist = torch.histogram(x, self.n_bins)
        return self._hist.bin_edges

    def _bins_from_quantiles(self, x: torch.Tensor) -> torch.Tensor:
        self._hist = None
        min = torch.min(x).reshape(-1)
        max = torch.max(x).reshape(-1)
        quantiles = torch.quantile(
            x, q=torch.tensor([i / self.n_bins for i in range(1, self.n_bins)])
        )
        return torch.cat((min, quantiles, max))

    def get_midpoints(self) -> Iterator[float]:
        for left, right in zip(self.bin_edges, self.bin_edges[1:]):
            yield (left + right) / 2

    def values_to_bin_indices(
        self, x_values: torch.Tensor, one_hot: bool = False
    ) -> torch.Tensor:
        binned_x = torch.zeros_like(x_values, dtype=torch.long)
        bin_number = 1
        for left, right in zip(self.bin_edges[1:], self.bin_edges[2:]):
            mask = (left < x_values) & (x_values <= right)
            binned_x[mask] = bin_number
            bin_number += 1

        if one_hot:
            return F.one_hot(binned_x, self.n_bins)
        else:
            return binned_x

    def bin_index_to_midpoint_value(self, bin_index: int) -> float:
        return (
            self.bin_edges[bin_index] + self.bin_edges[bin_index + 1]
        ).item() / 2


class ExposureCategoricalMLP(MLP):
    def __init__(
        self,
        binning: Binning,
        input_size: int,
        hidden: Iterable[int],
        lr: float,
        weight_decay: float = 0,
        add_input_layer_batchnorm: bool = False,
        add_hidden_layer_batchnorm: bool = False,
        activations: Iterable[nn.Module] = [nn.GELU()],
    ):
        super().__init__(
            input_size=input_size,
            hidden=hidden,
            out=binning.n_bins,
            add_input_layer_batchnorm=add_input_layer_batchnorm,
            add_hidden_layer_batchnorm=add_hidden_layer_batchnorm,
            activations=activations,
            lr=lr,
            weight_decay=lr,
            loss=F.cross_entropy,
        )
        self.binning = binning
        self.temperature = nn.Parameter(torch.tensor(1.0), requires_grad=False)

    def forward(self, xs):
        return super().forward(xs) / self.temperature

    def _step(self, batch, batch_index, log_prefix):
        x, _, ivs, covars = batch
        x_hat = self.forward(
            torch.hstack(
                [tens for tens in (ivs, covars) if tens.numel() > 0]
            )
        )

        loss = self.loss(x_hat, x)

        self.log(f"exposure_{log_prefix}_loss", loss)

        return loss


class OutcomeWithBinsMLP(MLP):
    def __init__(
        self,
        exposure_network: pl.LightningModule,
        input_size: int,
        hidden: Iterable[int],
        lr: float,
        weight_decay: float = 0,
        add_input_layer_batchnorm: bool = False,
        add_hidden_layer_batchnorm: bool = False,
        activations: Iterable[nn.Module] = [nn.LeakyReLU()],
    ):
        super().__init__(
            input_size=input_size,
            hidden=hidden,
            out=1,
            add_input_layer_batchnorm=add_input_layer_batchnorm,
            add_hidden_layer_batchnorm=add_hidden_layer_batchnorm,
            activations=activations,
            lr=lr,
            weight_decay=weight_decay,
            loss=F.mse_loss,
            _save_hyperparams=False,
        )
        self.exposure_network = exposure_network
        self.save_hyperparameters(ignore=["exposure_network"])

    def _step(self, batch, batch_index, log_prefix):
        _, y, ivs, covars = batch
        y_hat = self.forward(ivs, covars)
        loss = self.loss(y_hat, y)

        self.log(f"outcome_{log_prefix}_loss", loss)

        return loss

    def x_to_y(self, x_one_hot: torch.Tensor, covars: Optional[torch.Tensor]):
        if covars is not None:
            x = torch.hstack((x_one_hot, covars))
        else:
            x = x_one_hot

        return self.mlp(x)

    def forward(self, ivs, covars):
        # x is the input to the exposure model.
        mb = ivs.shape[0]
        exposure_net_xs = torch.hstack(
            [tens for tens in (ivs, covars) if tens is not None]
        )

        with torch.no_grad():
            exposure_probs = F.softmax(
                self.exposure_network.forward(exposure_net_xs), dim=1
            )

        y_hats = torch.zeros(mb, 1, device=self.device)
        for i in range(exposure_probs.shape[1]):
            weights = exposure_probs[:, [i]]

            cur_one_hot = F.one_hot(
                torch.tensor([i], device=self.device),
                self.exposure_network.binning.n_bins,
            ).repeat(mb, 1)

            pred = self.x_to_y(cur_one_hot, covars)

            y_hats += weights * pred

        return y_hats


class BinIVEstimator(MREstimator):
    def __init__(
        self,
        exposure_network: ExposureCategoricalMLP,
        outcome_network: OutcomeWithBinsMLP,
        binning: Binning,
    ):
        self.exposure_network = exposure_network
        self.outcome_network = outcome_network
        self.binning = binning

    def effect(
        self, x: torch.Tensor, covars: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        """Mean exposure to outcome effect at values of x."""
        # Get the bin for the provided xs.
        bins = self.binning.values_to_bin_indices(x, one_hot=True).to(
            torch.float32
        )

        if covars is None:
            # No covariables in the dataset or provided as arguments.
            # This will fail if covariables are necessary to go through the
            # network. The error won't be nice, so it will be better to catch
            # that. TODO
            with torch.no_grad():
                ys = self.outcome_network.x_to_y(bins, None)

            return ys.reshape(-1)

        else:
            n_cov_rows = covars.size(0)

            x_one_hot = torch.repeat_interleave(bins, n_cov_rows, dim=0)
            covars = covars.repeat(bins.size(0), 1)
            with torch.no_grad():
                y_hats = self.outcome_network.x_to_y(x_one_hot, covars)

            means = torch.tensor(
                [tens.mean() for tens in torch.split(y_hats, n_cov_rows)]
            )
            return means

    @classmethod
    def from_results(cls, directory_name: str) -> "BinIVEstimator":
        """Load a BinIV estimator instance from the directory containing the
        generated results.

        """
        with open(os.path.join(directory_name, "binning.pkl"), "rb") as f:
            binning = pickle.load(f)

        exposure_network = ExposureCategoricalMLP.load_from_checkpoint(
            os.path.join(directory_name, "exposure_network.ckpt")
        )

        outcome_network = OutcomeWithBinsMLP.load_from_checkpoint(
            os.path.join(directory_name, "outcome_network.ckpt"),
            exposure_network=exposure_network,
        )

        return cls(exposure_network, outcome_network, binning)


def main(args: argparse.Namespace) -> None:
    """Command-line interface entry-point."""
    validate_args(args)

    # Read the data.
    expected_cols = [args.exposure, args.outcome]
    expected_cols += args.instruments
    expected_cols += args.covariables

    data = read_data(args.data, args.sep, expected_cols)

    # Read genetic data if needed.
    if args.genotypes_backend is not None:
        backend_class = BACKENDS.get(
            args.genotypes_backend_type, GeneticDatasetBackend
        )
        backend: Optional[GeneticDatasetBackend] = backend_class.load(
            args.genotypes_backend
        )

    else:
        backend = None

    # Prepare train and validation datasets.
    # There is theoretically a little bit of leakage here because the histogram
    # or quantiles will be calculated including the validation dataset.
    # This should not have a big impact...
    dataset, binning = get_dataset_and_binning(
        args,
        data,
        backend,
        exposure=args.exposure,
        outcome=args.outcome,
        covariables=args.covariables,
        instruments=args.instruments,
    )

    # Automatically add the model hyperparameters.
    kwargs = {k: v for k, v in vars(args).items() if k in DEFAULTS.keys()}

    fit_bin_iv(
        dataset=dataset,
        binning=binning,
        backend=backend,
        no_plot=args.no_plot,
        **kwargs,
    )


def fit_bin_iv(
    dataset: Dataset,
    binning: Binning,
    backend: Optional[GeneticDatasetBackend] = None,
    output_dir: str = DEFAULTS["output_dir"],  # type: ignore
    validation_proportion: float = DEFAULTS["validation_proportion"],  # type: ignore # noqa: E501
    no_plot: bool = False,
    exposure_hidden: List[int] = DEFAULTS["exposure_hidden"],  # type: ignore
    exposure_learning_rate: float = DEFAULTS["exposure_learning_rate"],  # type: ignore # noqa: E501
    exposure_weight_decay: float = DEFAULTS["exposure_weight_decay"],  # type: ignore # noqa: E501
    exposure_batch_size: int = DEFAULTS["exposure_batch_size"],  # type: ignore
    exposure_max_epochs: int = DEFAULTS["exposure_max_epochs"],  # type: ignore
    outcome_hidden: List[int] = DEFAULTS["outcome_hidden"],  # type: ignore
    outcome_learning_rate: float = DEFAULTS["outcome_learning_rate"],  # type: ignore # noqa: E501
    outcome_weight_decay: float = DEFAULTS["outcome_weight_decay"],  # type: ignore # noqa: E501
    outcome_batch_size: int = DEFAULTS["outcome_batch_size"],  # type: ignore
    outcome_max_epochs: int = DEFAULTS["outcome_max_epochs"],  # type: ignore
    accelerator: str = DEFAULTS["accelerator"],  # type: ignore
) -> BinIVEstimator:
    # Create output directory if needed.
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    # Save binning.
    with open(os.path.join(output_dir, "binning.pkl"), "wb") as f:
        pickle.dump(binning, f)

    covars = save_covariables(
        dataset, os.path.join(output_dir, "covariables.pt")
    )

    # Split here into train and val.
    train_dataset, val_dataset = random_split(
        dataset, [1 - validation_proportion, validation_proportion]
    )

    datum = train_dataset[0]  # x, y, iv, covars
    n_exog = datum[2].numel() + datum[3].numel()

    train_exposure_model(
        train_dataset=train_dataset,
        val_dataset=val_dataset,
        binning=binning,
        backend=backend,
        input_size=n_exog,
        output_dir=output_dir,
        hidden=exposure_hidden,
        learning_rate=exposure_learning_rate,
        weight_decay=exposure_weight_decay,
        batch_size=exposure_batch_size,
        max_epochs=exposure_max_epochs,
        accelerator=accelerator,
    )

    exposure_network = ExposureCategoricalMLP.load_from_checkpoint(
        os.path.join(output_dir, "exposure_network.ckpt")
    ).eval()  # type: ignore

    # Apply temperature scaling to the exposure model to improve calibration.
    def _batch_fwd(model, batch):
        _, _, ivs, covars = batch
        return model.forward(torch.hstack((ivs, covars)))

    temperature_scale(
        exposure_network,
        val_dataset,
        batch_forward=_batch_fwd,
        batch_target=lambda batch: batch[0],
    )

    info(
        f"Temperature scaling parameter after tuning: "
        f"{exposure_network.temperature.item()}"
    )

    if not no_plot:
        plot_exposure_model(
            binning,
            exposure_network,
            val_dataset,
            output_filename=os.path.join(
                output_dir, "exposure_model_confusion_matrix.png"
            ),
        )

    train_outcome_model(
        train_dataset=train_dataset,
        val_dataset=val_dataset,
        binning=binning,
        exposure_network=exposure_network,
        output_dir=output_dir,
        hidden=outcome_hidden,
        learning_rate=outcome_learning_rate,
        weight_decay=outcome_weight_decay,
        batch_size=outcome_batch_size,
        max_epochs=outcome_max_epochs,
        accelerator=accelerator,
    )

    outcome_network = OutcomeWithBinsMLP.load_from_checkpoint(
        os.path.join(output_dir, "outcome_network.ckpt"),
        exposure_network=exposure_network,
    ).eval()  # type: ignore

    estimator = BinIVEstimator(exposure_network, outcome_network, binning)

    save_estimator_statistics(
        estimator,
        covars,
        output_prefix=os.path.join(output_dir, "causal_estimates"),
    )

    return estimator


@torch.no_grad()
def plot_exposure_model(
    binning: Binning,
    exposure_network: ExposureCategoricalMLP,
    val_dataset: Dataset,
    output_filename: str = "exposure_model_confusion_matrix.png",
):
    assert hasattr(val_dataset, "__len__")
    dataloader = DataLoader(val_dataset, batch_size=len(val_dataset))
    actual_bin, _, z, covariables = next(iter(dataloader))

    input = torch.hstack(
        [tens for tens in (z, covariables) if tens is not None]
    )
    predicted_bin = torch.argmax(
        F.softmax(exposure_network.forward(input), dim=1), dim=1
    )

    info(
        "Exposure model accuracy: {}".format(
            torch.mean((predicted_bin == actual_bin).to(torch.float32))
        )
    )

    confusion = ConfusionMatrix(
        task="multiclass", num_classes=binning.n_bins, normalize="true"
    )
    confusion_matrix = confusion(predicted_bin, actual_bin)  # type: ignore

    plt.figure(figsize=(10, 10))
    plt.matshow(confusion_matrix)
    plt.xlabel("Predicted bin")
    plt.ylabel("True bin")
    plt.colorbar()
    plt.savefig(output_filename, dpi=400)
    plt.clf()
    plt.close()


def save_estimator_statistics(
    estimator: BinIVEstimator,
    covars: Optional[torch.Tensor],
    output_prefix: str = "causal_estimates",
):
    # Save the causal effect at every bin midpoint.
    xs = torch.tensor(list(estimator.binning.get_midpoints()))
    ys = estimator.effect(xs, covars)
    plt.figure()
    plt.scatter(xs.numpy(), ys.numpy(), s=3)

    binning = estimator.binning
    for left, right in zip(binning.bin_edges, binning.bin_edges[1:]):
        plt.axvline(x=left, ls="--", lw=0.1, color="black")
        plt.axvline(x=right, ls="--", lw=0.1, color="black")

    plt.xlabel("X")
    plt.ylabel("Y")
    plt.savefig(f"{output_prefix}.png", dpi=600)
    plt.clf()

    df = pd.DataFrame({"x": xs, "y_do_x": ys})
    df.to_csv(f"{output_prefix}.csv", index=False)


def save_covariables(
    dataset: Dataset, output_filename: str
) -> Optional[torch.Tensor]:
    dl = DataLoader(dataset, batch_size=len(dataset))  # type: ignore
    covars = next(iter(dl))[3]
    if covars.shape[1] == 0:
        return None

    torch.save(covars, output_filename)
    return covars


def validate_args(args: argparse.Namespace) -> None:
    if args.genotypes_backend is not None and args.sample_id_col is None:
        critical(
            "When providing a genotypes dataset for the instrument, a "
            "sample id column needs to be provided using --sample-id-col "
            "so that the individuals can be matched between the genotypes "
            "and data file."
        )
        sys.exit(1)

    if args.validation_proportion < 0 or args.validation_proportion > 1:
        critical("--validation-proportion should be between 0 and 1.")
        sys.exit(1)


def get_dataset_and_binning(
    args: argparse.Namespace,
    data: pd.DataFrame,
    backend: Optional[GeneticDatasetBackend],
    exposure: str,
    outcome: str,
    covariables: Iterable[str],
    instruments: Iterable[str],
) -> Tuple[Dataset, Binning]:
    # If we have a backend, we need to add the genotypes to the exogenous
    # variable.
    if backend is not None:
        genetic_dataset = PhenotypeGeneticDataset(
            backend,
            data,
            args.sample_id_col,
            # We access columns manually later, so for now we ask for
            # everything we need through the exogenous columns.
            exogenous_columns=itertools.chain(
                instruments, covariables, [exposure, outcome]
            ),
        )
        return _dataset_from_genetic_dataset(
            args, genetic_dataset, exposure, outcome, covariables, instruments
        )

    else:
        exposure_tens = torch.from_numpy(data[exposure].values)
        outcome_tens = torch.from_numpy(data[[outcome]].values).to(
            torch.float32
        )

        binning = Binning(
            exposure_tens,
            mode="histogram" if args.histogram else "quantiles",
            n_bins=args.n_bins,
        )

        bins = binning.values_to_bin_indices(exposure_tens)
        instruments = torch.from_numpy(data[instruments].values).to(
            torch.float32
        )
        covariables = torch.from_numpy(data[covariables].values).to(
            torch.float32
        )

        class _Dataset(Dataset):
            def __getitem__(self, index):
                exposure = bins[index]
                outcome = outcome_tens[index]
                z = instruments[index]
                cur_covars = covariables[index]
                return exposure, outcome, z, cur_covars

            def __len__(self) -> int:
                return data.shape[0]

        return _Dataset(), binning


def _dataset_from_genetic_dataset(
    args: argparse.Namespace,
    genetic_dataset: PhenotypeGeneticDataset,
    exposure: str,
    outcome: str,
    covariables: Iterable[str],
    instruments: Iterable[str],
) -> Tuple[Dataset, Binning]:
    # Create binning.
    binning = Binning(
        genetic_dataset.exog[exposure],
        mode="histogram" if args.histogram else "quantiles",
        n_bins=args.n_bins,
    )

    instruments_set = set(instruments)
    covariables_set = set(covariables)

    instrument_idx = []
    covariable_idx = []
    exposure_idx = None
    outcome_idx = None
    for idx, col in enumerate(genetic_dataset.exogenous_columns):
        if col in instruments_set:
            instrument_idx.append(idx)
        if col in covariables_set:
            covariable_idx.append(idx)
        if col == exposure:
            assert exposure_idx is None
            exposure_idx = idx
        if col == outcome:
            assert outcome_idx is None
            outcome_idx = idx

    instrument_idx_tens = torch.tensor(instrument_idx)
    covariable_idx_tens = torch.tensor(covariable_idx)

    class _Dataset(Dataset):
        def __getitem__(self, index: int):
            # Get the binned exposure.
            cur = genetic_dataset[index]
            bin_exposure = binning.values_to_bin_indices(
                cur.exogenous[:, exposure_idx], one_hot=True
            )

            outcome = cur.exogenous[:, [outcome_idx]]
            instruments = cur.dosage
            covars = None

            if covariable_idx:
                covars = cur.exogenous[:, covariable_idx_tens]

            if instrument_idx:
                instruments = torch.hstack(
                    (instruments, cur.exogenous[:, instrument_idx_tens])
                )

            return bin_exposure, outcome, instruments, covars

        def __len__(self):
            return len(genetic_dataset)

    return _Dataset(), binning


def train_exposure_model(
    train_dataset: Dataset,
    val_dataset: Dataset,
    binning: Binning,
    backend: Optional[GeneticDatasetBackend],
    input_size: int,
    output_dir: str,
    hidden: List[int],
    learning_rate: float,
    weight_decay: float,
    batch_size: int,
    max_epochs: int,
    accelerator: Optional[str] = None,
) -> None:
    info("Training exposure model.")
    model = ExposureCategoricalMLP(
        binning=binning,
        input_size=input_size,
        hidden=hidden,
        lr=learning_rate,
        weight_decay=weight_decay,
        add_input_layer_batchnorm=True,
        add_hidden_layer_batchnorm=True,
    )

    train_dataloader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=4,
    )

    val_dataloader = DataLoader(
        val_dataset, batch_size=len(val_dataset), num_workers=4  # type: ignore
    )

    # Remove checkpoint if exists.
    full_filename = os.path.join(output_dir, "exposure_network.ckpt")
    if os.path.isfile(full_filename):
        info(f"Removing file '{full_filename}'.")
        os.remove(full_filename)

    trainer = pl.Trainer(
        log_every_n_steps=1,
        max_epochs=max_epochs,
        accelerator=accelerator,
        callbacks=[
            pl.callbacks.EarlyStopping(
                monitor="exposure_val_loss", patience=20
            ),
            pl.callbacks.ModelCheckpoint(
                filename="exposure_network",
                dirpath=output_dir,
                save_top_k=1,
                monitor="exposure_val_loss",
            ),
        ],
    )
    trainer.fit(model, train_dataloader, val_dataloader)


def train_outcome_model(
    train_dataset: Dataset,
    val_dataset: Dataset,
    binning: Binning,
    exposure_network: ExposureCategoricalMLP,
    output_dir: str,
    hidden: List[int],
    learning_rate: float,
    weight_decay: float,
    batch_size: int,
    max_epochs: int,
    accelerator: Optional[str] = None,
) -> None:
    info("Training outcome model.")
    n_covars = train_dataset[0][3].numel()
    model = OutcomeWithBinsMLP(
        exposure_network=exposure_network,
        input_size=binning.n_bins + n_covars,
        lr=learning_rate,
        weight_decay=weight_decay,
        hidden=hidden,
    )

    train_dataloader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=4,
    )

    val_dataloader = DataLoader(
        val_dataset, batch_size=len(val_dataset), num_workers=4  # type: ignore
    )

    # Remove checkpoint if exists.
    full_filename = os.path.join(output_dir, "outcome_network.ckpt")
    if os.path.isfile(full_filename):
        info(f"Removing file '{full_filename}'.")
        os.remove(full_filename)

    trainer = pl.Trainer(
        log_every_n_steps=1,
        max_epochs=max_epochs,
        accelerator=accelerator,
        callbacks=[
            pl.callbacks.EarlyStopping(
                monitor="outcome_val_loss", patience=20
            ),
            pl.callbacks.ModelCheckpoint(
                filename="outcome_network",
                dirpath=output_dir,
                save_top_k=1,
                monitor="outcome_val_loss",
            ),
        ],
    )
    trainer.fit(model, train_dataloader, val_dataloader)


def configure_argparse(parser) -> None:
    parser.add_argument(
        "--n-bins",
        type=int,
        help="Number of bins used for density estimation in the "
        "exposure model.",
        required=True,
    )

    parser.add_argument(
        "--histogram",
        action="store_true",
        help="By default, we use quantiles for density estimation. Using this "
        "option, we will use evenly spaced bins (histogram) instead.",
    )

    parser.add_argument("--output-dir", default=DEFAULTS["output_dir"])

    parser.add_argument(
        "--genotypes-backend",
        help=(
            "Pickle containing a pytorch-genotypes backend. This can be "
            "created from various genetic data formats using the "
            "'pt-geno-create-backend' command line utility provided by "
            "pytorch genotypes."
        ),
        type=str,
    )

    parser.add_argument(
        "--genotypes-backend-type",
        help=(
            "Pickle containing a pytorch-genotypes backend. This can be "
            "created from various genetic data formats using the "
            "'pt-geno-create-backend' command line utility provided by "
            "pytorch genotypes."
        ),
        type=str,
    )

    parser.add_argument(
        "--no-plot",
        help="Disable plotting of diagnostics.",
        action="store_true",
    )

    parser.add_argument(
        "--data", "-d", required=True, help="Path to a data file."
    )

    parser.add_argument(
        "--sep",
        default="\t",
        help="Separator (column delimiter) for the data file.",
    )

    parser.add_argument(
        "--instruments",
        "-z",
        nargs="*",
        default=[],
        help="The instrument (Z or G) in the case where we're not using "
        "genotypes provided through --genotypes. Multiple values can "
        "be provided for multiple instruments.\n"
        "This should be column(s) in the data file.",
    )

    parser.add_argument(
        "--covariables",
        nargs="*",
        default=[],
        help="Variables which will be included in both stages."
        "This should be column(s) in the data file.",
    )

    parser.add_argument(
        "--exposure",
        "-x",
        help="The exposure (X). This should be a column name in the data "
        "file.",
        required=True,
        type=str,
    )

    parser.add_argument(
        "--outcome",
        "-y",
        help="The outcome (Y). This should be a column name in the data "
        "file.",
        required=True,
        type=str,
    )

    parser.add_argument(
        "--outcome-type",
        default="continuous",
        choices=["continuous", "binary"],
        help="Variable type for the outcome (binary vs continuous).",
    )

    parser.add_argument(
        "--sample-id-col",
        default="sample_id",
        help="Column that contains the sample id. This is mandatory if "
        "genotypes are provided to enable joining.",
    )

    parser.add_argument(
        "--validation-proportion",
        type=float,
        default=DEFAULTS["validation_proportion"],
    )

    parser.add_argument(
        "--accelerator",
        default=DEFAULTS["accelerator"],
        help="Accelerator (e.g. gpu, cpu, mps) use to train the model. This "
        "will be passed to Pytorch Lightning.",
    )

    MLP.add_argparse_parameters(
        parser,
        "exposure-",
        "Exposure Model Parameters",
        defaults={
            "hidden": DEFAULTS["exposure_hidden"],
            "batch-size": DEFAULTS["exposure_batch_size"],
        },
    )

    MLP.add_argparse_parameters(
        parser,
        "outcome-",
        "Outcome Model Parameters",
        defaults={
            "hidden": DEFAULTS["outcome_hidden"],
            "batch-size": DEFAULTS["outcome_batch_size"],
        },
    )


# Standard names for estimators.
create_estimator = fit_bin_iv
load_estimator = BinIVEstimator.from_results
