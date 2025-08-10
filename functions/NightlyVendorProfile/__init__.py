import logging
import azure.functions as func
# You can expand this to recompute per-vendor stats in Cosmos or push summaries to Search

async def main(mytimer: func.TimerRequest) -> None:
    logging.info("Nightly vendor profile refresh ran.")
