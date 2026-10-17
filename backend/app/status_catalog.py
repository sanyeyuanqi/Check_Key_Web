"""Initial status values shared with the desktop checker's result vocabulary."""

STATUS_CATALOG = (
    ("valid", "可用", "valid"),
    ("usage_limit_reached", "额度待恢复", "recovery"),
    ("insufficient_credit", "余额不足", "insufficient"),
    ("invalid", "无效", "invalid"),
    ("permission_error", "权限不足", "invalid"),
    ("rate_limited", "请求限速", "recovery"),
    ("billing_error", "账单异常", "insufficient"),
    ("request_or_spend_limit_error", "请求/额度限制", "recovery"),
    ("expired", "凭证过期", "invalid"),
    ("invalid_signature", "签名错误", "invalid"),
    ("model_unavailable", "模型不可用", "invalid"),
    ("no_models", "无可用模型", "invalid"),
    ("network_error", "网络异常", "neutral"),
    ("server_error", "服务异常", "neutral"),
    ("invalid_response", "响应异常", "neutral"),
    ("http_error", "接口异常", "neutral"),
    ("internal_error", "检测异常", "neutral"),
)


def initial_status_rows() -> list[dict[str, object]]:
    return [
        {"value": value, "name": name, "tone": tone, "sort_order": index}
        for index, (value, name, tone) in enumerate(STATUS_CATALOG)
    ]
