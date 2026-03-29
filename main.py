import click
from config import build_config_from_args, load_env

@click.command()
@click.option("--protocol", required=True)
@click.option("--seed-dir", required=True)
@click.option("--rfc-path", required=True)
@click.option("--target", required=False, default="aflnet")
def main(protocol: str, seed_dir: str, rfc_path: str, target: str):
    build_config_from_args(protocol, seed_dir, rfc_path)
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