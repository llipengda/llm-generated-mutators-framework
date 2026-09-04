import click
from config import build_config_from_args, load_env

@click.command()
@click.option("--protocol", required=True)
@click.option("--seed-dir", required=True)
@click.option("--rfc-path", "rfc_paths", required=True, multiple=True, help="Path(s) to RFC document(s). Can be specified multiple times for multiple RFCs.")
@click.option("--fixer", is_flag=True, default=False, help="Enable fixer generation and validation.")
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
    fixer: bool,
    repair_datamodel_assembly: bool,
):
    build_config_from_args(protocol, seed_dir, list(rfc_paths), fixer=fixer)
    load_env()

    from pipeline.peach import PeachPipeline

    pipeline = PeachPipeline()
    if repair_datamodel_assembly:
        pipeline.repair_datamodel_assembly()
        return

    pipeline()

if __name__ == "__main__":
    main()
