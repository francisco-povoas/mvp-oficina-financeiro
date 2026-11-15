"""Aguarda o ElasticMQ e cria, de forma idempotente, a fila usada no CI."""

from __future__ import annotations

import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

QUEUE_NAME = "mvp-oficina-financeiro-to-ordens-local.fifo"


def main() -> None:
    client = boto3.client(
        "sqs", region_name="us-east-1", endpoint_url="http://127.0.0.1:9324"
    )
    last_error: Exception | None = None
    for _ in range(30):
        try:
            client.list_queues()
            break
        except (BotoCoreError, ClientError) as exc:
            last_error = exc
            time.sleep(1)
    else:
        raise RuntimeError("ElasticMQ não ficou saudável") from last_error

    client.create_queue(
        QueueName=QUEUE_NAME,
        Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
    )
    observed = {
        url.rsplit("/", 1)[-1] for url in client.list_queues().get("QueueUrls", [])
    }
    if QUEUE_NAME not in observed:
        raise RuntimeError(f"Fila ausente após bootstrap: {QUEUE_NAME}")
    print("ElasticMQ saudável; fila inicializada")


if __name__ == "__main__":
    main()
