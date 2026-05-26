import click
from config import build_config_from_args, load_env

@click.command()
@click.option("--protocol", required=True)
@click.option("--seed-dir", required=True)
@click.option("--rfc-path", required=True)
@click.option("--target", required=False, default="aflnet")
@click.option("--fixer", is_flag=True, default=False, help="Enable fixer generation and validation (Peach only).")
def main(protocol: str, seed_dir: str, rfc_path: str, target: str, fixer: bool):
    build_config_from_args(protocol, seed_dir, rfc_path, fixer=fixer)
    load_env()

    if target == "aflnet":
        from pipeline.aflnet import AFLNetPipeline
        pipeline = AFLNetPipeline()
    elif target == "peach":
        from pipeline.peach import PeachPipeline
        pipeline = PeachPipeline()
    else:
        raise ValueError(f"Unknown target: {target}")
    
    pipeline()

if __name__ == "__main__":
    main()