import os
import sys
import typer

from pathlib import Path
from loguru import logger
from typing import Optional
from enum import Enum
from huggingface_hub import hf_hub_download


app = typer.Typer()


class Quantization(str, Enum):
    bitsandbytes = "bitsandbytes"
    bitsandbytes_nf4 = "bitsandbytes-nf4"
    bitsandbytes_fp4 = "bitsandbytes-fp4"
    gptq = "gptq"
    awq = "awq"
    eetq = "eetq"


class Dtype(str, Enum):
    float16 = "float16"
    bloat16 = "bfloat16"


@app.command()
def serve(
    model_id: str,
    revision: Optional[str] = None,
    sharded: bool = False,
    quantize: Optional[Quantization] = None,
    speculate: Optional[int] = None,
    dtype: Optional[Dtype] = None,
    trust_remote_code: bool = False,
    uds_path: Path = "/tmp/text-generation-server",
    logger_level: str = "INFO",
    json_output: bool = False,
    otlp_endpoint: Optional[str] = None,
):
    if sharded:
        assert (
            os.getenv("RANK", None) is not None
        ), "RANK must be set when sharded is True"
        assert (
            os.getenv("WORLD_SIZE", None) is not None
        ), "WORLD_SIZE must be set when sharded is True"
        assert (
            os.getenv("MASTER_ADDR", None) is not None
        ), "MASTER_ADDR must be set when sharded is True"
        assert (
            os.getenv("MASTER_PORT", None) is not None
        ), "MASTER_PORT must be set when sharded is True"

    # Remove default handler
    logger.remove()
    logger.add(
        sys.stdout,
        format="{message}",
        filter="text_generation_server",
        level=logger_level,
        serialize=json_output,
        backtrace=True,
        diagnose=False,
    )

    # Import here after the logger is added to log potential import exceptions
    from text_generation_server import server
    from text_generation_server.tracing import setup_tracing

    # Setup OpenTelemetry distributed tracing
    if otlp_endpoint is not None:
        setup_tracing(shard=os.getenv("RANK", 0), otlp_endpoint=otlp_endpoint)

    # Downgrade enum into str for easier management later on
    quantize = None if quantize is None else quantize.value
    dtype = None if dtype is None else dtype.value
    if dtype is not None and quantize not in {
        None,
        "bitsandbytes",
        "bitsandbytes-nf4",
        "bitsandbytes-fp4",
    }:
        raise RuntimeError(
            "Only 1 can be set between `dtype` and `quantize`, as they both decide how goes the final model."
        )
    server.serve(
        model_id,
        revision,
        sharded,
        quantize,
        speculate,
        dtype,
        trust_remote_code,
        uds_path,
    )


@app.command()
def download_weights(
    model_id: str,
    revision: Optional[str] = None,
    extension: str = ".safetensors",
    auto_convert: bool = True,
    logger_level: str = "INFO",
    json_output: bool = False,
    trust_remote_code: bool = False,
):
    # Remove default handler
    logger.remove()
    logger.add(
        sys.stdout,
        format="{message}",
        filter="text_generation_server",
        level=logger_level,
        serialize=json_output,
        backtrace=True,
        diagnose=False,
    )

    # Import here after the logger is added to log potential import exceptions
    from text_generation_server import utils

    # Test if files were already download
    try:
        utils.weight_files(model_id, revision, extension)
        logger.info("Files are already present on the host. " "Skipping download.")
        return
    # Local files not found
    except (utils.LocalEntryNotFoundError, FileNotFoundError, utils.EntryNotFoundError):
        pass

    is_local_model = (Path(model_id).exists() and Path(model_id).is_dir()) or os.getenv(
        "WEIGHTS_CACHE_OVERRIDE", None
    ) is not None

    if not is_local_model:
        try:
            adapter_config_filename = hf_hub_download(
                model_id, revision=revision, filename="adapter_config.json"
            )
            utils.download_and_unload_peft(
                model_id, revision, trust_remote_code=trust_remote_code
            )
            is_local_model = True
            utils.weight_files(model_id, revision, extension)
            return
        except (utils.LocalEntryNotFoundError, utils.EntryNotFoundError):
            pass

        try:
            import json

            medusa_head = hf_hub_download(
                model_id, revision=revision, filename="medusa_lm_head.pt"
            )
            if auto_convert:
                medusa_sf = Path(medusa_head[: -len(".pt")] + ".safetensors")
                if not medusa_sf.exists():
                    utils.convert_files([Path(medusa_head)], [medusa_sf], [])
            medusa_config = hf_hub_download(
                model_id, revision=revision, filename="config.json"
            )
            with open(medusa_config, "r") as f:
                config = json.load(f)

            model_id = config["base_model_name_or_path"]
            revision = "main"
            try:
                utils.weight_files(model_id, revision, extension)
                logger.info(
                    f"Files for parent {model_id} are already present on the host. "
                    "Skipping download."
                )
                return
            # Local files not found
            except (
                utils.LocalEntryNotFoundError,
                FileNotFoundError,
                utils.EntryNotFoundError,
            ):
                pass
        except (utils.LocalEntryNotFoundError, utils.EntryNotFoundError):
            pass

        # Try to download weights from the hub
        try:
            filenames = utils.weight_hub_files(model_id, revision, extension)
            utils.download_weights(filenames, model_id, revision)
            # Successfully downloaded weights
            return

        # No weights found on the hub with this extension
        except utils.EntryNotFoundError as e:
            # Check if we want to automatically convert to safetensors or if we can use .bin weights instead
            if not extension == ".safetensors" or not auto_convert:
                raise e

    else:
        # Try to load as a local PEFT model
        try:
            utils.download_and_unload_peft(
                model_id, revision, trust_remote_code=trust_remote_code
            )
            utils.weight_files(model_id, revision, extension)
            return
        except (utils.LocalEntryNotFoundError, utils.EntryNotFoundError):
            pass

    # Try to see if there are local pytorch weights
    try:
        # Get weights for a local model, a hub cached model and inside the WEIGHTS_CACHE_OVERRIDE
        local_pt_files = utils.weight_files(model_id, revision, ".bin")

    # No local pytorch weights
    except utils.LocalEntryNotFoundError:
        if extension == ".safetensors":
            logger.warning(
                f"No safetensors weights found for model {model_id} at revision {revision}. "
                f"Downloading PyTorch weights."
            )

        # Try to see if there are pytorch weights on the hub
        pt_filenames = utils.weight_hub_files(model_id, revision, ".bin")
        # Download pytorch weights
        local_pt_files = utils.download_weights(pt_filenames, model_id, revision)

    if auto_convert:
        logger.warning(
            f"No safetensors weights found for model {model_id} at revision {revision}. "
            f"Converting PyTorch weights to safetensors."
        )

        # Safetensors final filenames
        local_st_files = [
            p.parent / f"{p.stem.lstrip('pytorch_')}.safetensors"
            for p in local_pt_files
        ]
        try:
            import transformers
            import json

            if is_local_model:
                config_filename = os.path.join(model_id, "config.json")
            else:
                config_filename = hf_hub_download(
                    model_id, revision=revision, filename="config.json"
                )
            with open(config_filename, "r") as f:
                config = json.load(f)
            architecture = config["architectures"][0]

            class_ = getattr(transformers, architecture)

            # Name for this varible depends on transformers version.
            discard_names = getattr(class_, "_tied_weights_keys", [])

        except Exception as e:
            discard_names = []
        # Convert pytorch weights to safetensors
        utils.convert_files(local_pt_files, local_st_files, discard_names)


@app.command()
def quantize(
    model_id: str,
    output_dir: str,
    revision: Optional[str] = None,
    logger_level: str = "INFO",
    json_output: bool = False,
    trust_remote_code: bool = False,
    upload_to_model_id: Optional[str] = None,
    percdamp: float = 0.01,
    act_order: bool = False,
):
    if revision is None:
        revision = "main"
    download_weights(
        model_id=model_id,
        revision=revision,
        logger_level=logger_level,
        json_output=json_output,
    )
    from text_generation_server.utils.gptq.quantize import quantize

    quantize(
        model_id=model_id,
        bits=4,
        groupsize=128,
        output_dir=output_dir,
        revision=revision,
        trust_remote_code=trust_remote_code,
        upload_to_model_id=upload_to_model_id,
        percdamp=percdamp,
        act_order=act_order,
    )


if __name__ == "__main__":
    app()
