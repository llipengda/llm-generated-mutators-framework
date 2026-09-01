import click
from config import build_config_from_args, load_env

@click.command()
@click.option("--protocol", required=True)
@click.option("--seed-dir", required=True)
@click.option("--rfc-path", "rfc_paths", required=True, multiple=True, help="Path(s) to RFC document(s). Can be specified multiple times for multiple RFCs.")
@click.option("--target", required=False, default="aflnet")
@click.option("--fixer", is_flag=True, default=False, help="Enable fixer generation and validation (Peach only).")
@click.option(
    "--repair-datamodel-assembly",
    is_flag=True,
    default=False,
    help="Repair and compile existing Peach DataModel DSL modules without regenerating them.",
)
def main(
    protocol: str,
    seed_dir: str,
    rfc_paths: list[str],
    target: str,
    fixer: bool,
    repair_datamodel_assembly: bool,
):
    build_config_from_args(protocol, seed_dir, list(rfc_paths), fixer=fixer)
    load_env()

    if repair_datamodel_assembly and target != "peach":
        raise click.UsageError("--repair-datamodel-assembly requires --target peach")

    if target == "aflnet":
        from pipeline.aflnet import AFLNetPipeline
        pipeline = AFLNetPipeline()
    elif target == "peach":
        from pipeline.peach import PeachPipeline
        pipeline = PeachPipeline()
        if repair_datamodel_assembly:
            pipeline.repair_datamodel_assembly()
            return
    else:
        raise ValueError(f"Unknown target: {target}")

    pipeline()

if __name__ == "__main__":
    main()
