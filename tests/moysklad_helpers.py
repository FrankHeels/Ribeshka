"""Synthetic, internally consistent fixtures for MoySklad acceptance tests."""

import uuid
from decimal import Decimal


ACCOUNT_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = "test"
NAMESPACE = uuid.UUID("10000000-0000-4000-8000-000000000002")
UNIT_KG_ID = uuid.UUID("10000000-0000-4000-8000-000000000003")
UNIT_PIECE_ID = uuid.UUID("10000000-0000-4000-8000-000000000004")
PRICE_TYPE_ID = uuid.UUID("10000000-0000-4000-8000-000000000005")
CURRENCY_ID = uuid.UUID("10000000-0000-4000-8000-000000000006")
ORGANIZATION_ID = uuid.UUID("10000000-0000-4000-8000-000000000007")
AGENT_ID = uuid.UUID("10000000-0000-4000-8000-000000000008")
INITIAL_STATE_ID = uuid.UUID("10000000-0000-4000-8000-000000000009")
API_BASE = "https://api.moysklad.ru/api/remap/1.2/"
WEBHOOK_SECRET = "a" * 64


def moysklad_config(*, enabled=True):
    return {
        "account_id": str(ACCOUNT_ID),
        "environment": ENVIRONMENT,
        "namespace": str(NAMESPACE),
        "api_token": "synthetic-test-token",
        "price_type_id": str(PRICE_TYPE_ID),
        "currency_id": str(CURRENCY_ID),
        "unit_id": str(UNIT_KG_ID),
        "organization_id": str(ORGANIZATION_ID),
        "agent_id": str(AGENT_ID),
        "initial_state_id": str(INITIAL_STATE_ID),
        "webhook_secret": WEBHOOK_SECRET,
        "webhook_url": f"https://fish.example.test/integrations/moysklad/webhooks/prices/{WEBHOOK_SECRET}/",
        "catalog_enabled": enabled,
        "prices_enabled": enabled,
        "export_enabled": enabled,
        "statuses_enabled": enabled,
    }


MOYSKLAD_TEST_CONFIG = moysklad_config(enabled=True)


def external_product_id():
    return uuid.uuid4()


def product_payload(external_id=None, *, name="Треска", price="125.50", code="SKU-1",
                    unit_id=UNIT_KG_ID, currency_id=CURRENCY_ID, price_type_id=PRICE_TYPE_ID,
                    updated="2026-10-15 10:00:00.000", archived=False):
    external_id = external_id or external_product_id()
    return {
        "meta": {
            "href": f"{API_BASE}entity/product/{external_id}",
            "type": "product",
            "mediaType": "application/json",
        },
        "id": str(external_id),
        "accountId": str(ACCOUNT_ID),
        "name": name,
        "code": code,
        "archived": archived,
        "updated": updated,
        "uom": {"meta": {
            "href": f"{API_BASE}entity/uom/{unit_id}",
            "type": "uom",
            "mediaType": "application/json",
        }},
        "salePrices": [{
            "value": str(int(Decimal(str(price)) * 100)),
            "priceType": {"meta": {
                "href": f"{API_BASE}context/companysettings/pricetype/{price_type_id}",
                "type": "pricetype",
                "mediaType": "application/json",
            }},
            "currency": {"meta": {
                "href": f"{API_BASE}entity/currency/{currency_id}",
                "type": "currency",
                "mediaType": "application/json",
            }},
        }],
    }


def webhook_event(external_id, *, action="UPDATE", updated_fields=None, **extra):
    return {
        "accountId": str(ACCOUNT_ID),
        "meta": {
            "href": f"{API_BASE}entity/product/{external_id}",
            "type": "product",
            "mediaType": "application/json",
        },
        "action": action,
        "updatedFields": updated_fields or ["salePrices"],
        **extra,
    }
