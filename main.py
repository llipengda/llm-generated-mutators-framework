import click
from core.config import build_config_from_args, load_env
from core.log import log_session
from core.language import normalize_language

@click.command()
@click.option("--protocol", required=True)
@click.option("--seed-dir", required=True)
@click.option("--rfc-path", "rfc_paths", required=True, multiple=True, help="Path(s) to RFC document(s). Can be specified multiple times for multiple RFCs.")
@click.option("--language", type=normalize_language, metavar="LANGUAGE", default="en", show_default=True,
              help="Language for model reasoning and natural-language output (e.g. zh-CN, en, Japanese).")
def main(
    protocol: str,
    seed_dir: str,
    rfc_paths: list[str],
    language: str,
):
    with log_session(protocol):
        load_env()
        build_config_from_args(
            protocol,
            seed_dir,
            list(rfc_paths),
            language=language,
        )
        from pipeline.peach import PeachPipeline

        pipeline = PeachPipeline()
        pipeline()

if __name__ == "__main__":
    main()
