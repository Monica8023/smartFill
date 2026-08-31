import json

import httpx
import pytest

from smartfill.vision import (
    AliyunVisionProvider,
    BrowserObservation,
    VisionRequest,
    VisionResponseError,
)


@pytest.mark.asyncio
async def test_aliyun_provider_parses_a_schema_constrained_decision() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "page_type": "profile_form",
                                    "interruptions": [],
                                    "mappings": [
                                        {
                                            "canonical_field": "person.fullName",
                                            "element_id": "el-7",
                                            "confidence": 0.96,
                                            "evidence": "label=姓名",
                                        }
                                    ],
                                    "next_action": {
                                        "action": "fill",
                                        "element_id": "el-7",
                                        "value_ref": "record://person.fullName",
                                        "confidence": 0.96,
                                    },
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AliyunVisionProvider(api_key="test-key", http_client=client)
        result = await provider.analyze(
            VisionRequest(
                task="fill profile fields",
                observation=BrowserObservation(
                    url="https://target.example.com/profile",
                    screenshot_base64="aW1hZ2U=",
                    elements=[
                        {
                            "element_id": "el-7",
                            "role": "textbox",
                            "accessible_name": "姓名",
                        }
                    ],
                ),
                canonical_fields=["person.fullName"],
            )
        )

    assert result.mappings[0].element_id == "el-7"
    assert result.next_action is not None
    assert result.next_action.value_ref == "record://person.fullName"
    assert captured["authorization"] == "Bearer test-key"
    assert "test-key" not in json.dumps(captured["payload"])


@pytest.mark.asyncio
async def test_aliyun_provider_rejects_invalid_model_output() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AliyunVisionProvider(api_key="test-key", http_client=client)
        with pytest.raises(VisionResponseError, match="valid JSON"):
            await provider.analyze(
                VisionRequest(
                    task="fill",
                    observation=BrowserObservation(
                        url="https://target.example.com",
                        screenshot_base64="aW1hZ2U=",
                        elements=[],
                    ),
                    canonical_fields=[],
                )
            )


@pytest.mark.asyncio
async def test_aliyun_provider_rejects_hallucinated_elements_and_fields() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "page_type": "profile_form",
                                    "interruptions": [],
                                    "mappings": [
                                        {
                                            "canonical_field": "account.password",
                                            "element_id": "made-up-element",
                                            "confidence": 0.99,
                                            "evidence": "guessed",
                                        }
                                    ],
                                    "next_action": None,
                                }
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AliyunVisionProvider(api_key="test-key", http_client=client)
        with pytest.raises(VisionResponseError, match="unrecognized field or element"):
            await provider.analyze(
                VisionRequest(
                    task="fill",
                    observation=BrowserObservation(
                        url="https://target.example.com",
                        screenshot_base64="aW1hZ2U=",
                        elements=[
                            {
                                "element_id": "real-element",
                                "role": "textbox",
                                "accessible_name": "姓名",
                            }
                        ],
                    ),
                    canonical_fields=["person.fullName"],
                )
            )
