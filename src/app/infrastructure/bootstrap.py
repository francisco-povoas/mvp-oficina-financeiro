import time

from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings
from app.infrastructure.dynamodb import ensure_table


def main() -> None:
    settings = get_settings()
    last_error: Exception | None = None
    for _ in range(30):
        try:
            ensure_table(settings)
            return
        except (BotoCoreError, ClientError) as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError("DynamoDB Local não ficou disponível") from last_error


if __name__ == "__main__":
    main()
