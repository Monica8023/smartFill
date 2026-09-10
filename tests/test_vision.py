import json

import httpx
import pytest

from smartfill.vision import (
    AliyunVisionProvider,
    BrowserObservation,
    RequiredPageField,
    VisionDecision,
    VisionRequest,
    VisionResponseError,
    VisionUsage,
)


@pytest.mark.asyncio
async def test_aliyun_provider_parses_a_schema_constrained_decision() -> None:
    captured: dict[str, object] = {}
    usages: list[VisionUsage] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                },
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
                                        "evidence": "The visible name field is grounded as el-7",
                                        "accessible_name": "Name",
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
        provider = AliyunVisionProvider(
            api_key="test-key",
            http_client=client,
            usage_reporter=usages.append,
        )
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
                            "tag": "input",
                        }
                    ],
                ),
                canonical_fields=["person.fullName"],
            )
        )

    assert result.mappings[0].element_id == "el-7"
    assert result.next_action is None
    assert provider.guardrail_corrections == 1
    assert captured["authorization"] == "Bearer test-key"
    assert "test-key" not in json.dumps(captured["payload"])
    payload = captured["payload"]
    assert isinstance(payload, dict)
    system_prompt = payload["messages"][0]["content"]  # type: ignore[index]
    assert "Buttons and links are actions, never field mappings" in system_prompt
    assert usages == [
        VisionUsage(prompt_tokens=120, completion_tokens=30, total_tokens=150)
    ]


@pytest.mark.asyncio
async def test_aliyun_provider_drops_navigation_pseudo_fields_when_action_is_grounded() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "page_type": "service_catalog",
                                    "interruptions": [],
                                    "mappings": [],
                                    "target_reached": False,
                                    "required_fields": [
                                        {
                                            "key": "social_security_card_status_query",
                                            "display_name": "社会保障卡应用状态查询",
                                            "reason": "需要进入服务分类",
                                        }
                                    ],
                                    "next_action": {
                                        "action": "click",
                                        "element_id": "el-card",
                                        "confidence": 0.98,
                                        "evidence": "截图中可见社会保障卡分类",
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
                task="找到社会保障卡应用状态查询入口并点击",
                observation=BrowserObservation(
                    url="https://www.12333.gov.cn/portal/service_catalog",
                    screenshot_base64="aW1hZ2U=",
                    elements=[
                        {
                            "element_id": "el-card",
                            "role": "link",
                            "accessible_name": "社会保障卡",
                            "tag": "a",
                        }
                    ],
                ),
                canonical_fields=[],
            )
        )

    assert result.required_fields == []
    assert result.next_action is not None
    assert result.next_action.element_id == "el-card"
    assert provider.guardrail_corrections == 1


@pytest.mark.asyncio
async def test_aliyun_provider_regrounds_action_by_unique_accessible_name() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "page_type": "service_catalog",
                                    "interruptions": [],
                                    "mappings": [],
                                    "next_action": {
                                        "action": "click",
                                        "element_id": "service_catalog_social_security_card",
                                        "accessible_name": "社会保障卡",
                                        "confidence": 0.98,
                                        "evidence": "截图中可见唯一的社会保障卡分类",
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
                task="找到社会保障卡应用状态查询入口并点击",
                observation=BrowserObservation(
                    url="https://www.12333.gov.cn/portal/service_catalog",
                    screenshot_base64="aW1hZ2U=",
                    elements=[
                        {
                            "element_id": "vision-192",
                            "role": "link",
                            "accessible_name": "社会保障卡",
                            "tag": "a",
                        }
                    ],
                ),
                canonical_fields=[],
            )
        )

    assert result.next_action is not None
    assert result.next_action.element_id == "vision-192"
    assert provider.guardrail_corrections == 1


@pytest.mark.asyncio
async def test_aliyun_provider_rejects_invalid_model_output() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AliyunVisionProvider(api_key="test-key", http_client=client)
        with pytest.raises(
            VisionResponseError,
            match=r"valid JSON.*JSONDecodeError.*not json",
        ):
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


def test_vision_decision_can_request_missing_customer_profile_fields() -> None:
    decision = VisionDecision(
        page_type="property_verification_form",
        interruptions=[],
        mappings=[],
        target_reached=True,
        required_fields=[
            RequiredPageField(
                key="property.certificateNumber",
                display_name="房产证号",
                input_kind="text",
                sensitive=True,
                reason="visible required input has no matching customer profile value",
            )
        ],
    )

    assert decision.required_fields[0].key == "property.certificateNumber"
    assert decision.target_reached is True


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
