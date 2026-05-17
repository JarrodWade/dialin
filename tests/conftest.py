"""Moto-backed DynamoDB table for ddb.py / tools.py tests."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
LAMBDA_DIR = ROOT / "lambda"
if str(LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, str(LAMBDA_DIR))

TABLE_NAME = "dialin-test"


def _create_table() -> None:
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def dynamodb_env(monkeypatch: pytest.MonkeyPatch):
    """Fresh mocked table; reload ddb/tools so they bind to it."""
    monkeypatch.setenv("TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BEDROCK_EMBEDDING_MODEL_ID", raising=False)

    with mock_aws():
        _create_table()
        import ddb
        import tools

        importlib.reload(ddb)
        importlib.reload(tools)
        yield {"ddb": ddb, "tools": tools}
