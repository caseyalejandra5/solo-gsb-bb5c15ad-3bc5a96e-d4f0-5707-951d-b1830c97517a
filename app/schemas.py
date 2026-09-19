"""请求/响应模型与输入校验。"""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator

from .rfc1982 import MAX_SERIAL

Serial = Annotated[int, Field(ge=0, le=MAX_SERIAL, description="32 位无符号序列号")]

# DNS 区域名：标签由字母/数字/连字符组成，允许末尾根点
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ZONE_RE = re.compile(rf"^(?:{_LABEL}\.)+{_LABEL}?\.?$|^{_LABEL}\.?$")


def validate_zone_name(value: str) -> str:
    """校验并规范化 DNS 区域名（去除首尾空白）。"""
    zone = value.strip()
    if not _ZONE_RE.match(zone):
        raise ValueError("非法的 DNS 区域名")
    return zone


#: 路径参数中的区域名（非法时返回 422）
ZoneName = Annotated[str, AfterValidator(validate_zone_name)]


class AuditCreate(BaseModel):
    """提交一次审计：区域名 + 按观测时间升序排列的序列号列表。"""

    zone: str = Field(min_length=1, max_length=253, examples=["example.com"])
    serials: list[Serial] = Field(
        min_length=1,
        max_length=4096,
        description="按观测时间从早到晚排列的 SOA serial",
        examples=[[2026091800, 2026091801, 2026091801, 2026091705]],
    )

    @field_validator("zone")
    @classmethod
    def _check_zone(cls, value: str) -> str:
        return validate_zone_name(value)


class AuditItem(BaseModel):
    """单个观测点的判定结果。"""

    index: int
    serial: int
    relation: Literal["initial", "advance", "equal", "regression", "incomparable"]
    delta: int | None = Field(
        description="相对上一观测值的带符号距离；首项与不可比较时为 null"
    )
    slave_reject_risk: bool = Field(
        description="该节点是否可能使从服务器拒绝/无法应用更新"
    )
    note: str


class AuditSummary(BaseModel):
    total: int
    advance: int
    equal: int
    regression: int
    incomparable: int
    risk_indexes: list[int] = Field(description="存在从服务器拒绝风险的观测点下标")


class AuditOut(BaseModel):
    id: int
    zone: str
    created_at: str
    serials: list[int]
    items: list[AuditItem]
    summary: AuditSummary


class AuditListItem(BaseModel):
    id: int
    zone: str
    created_at: str
    total: int
    risk_count: int


class RepairPlanItem(BaseModel):
    """修复预演中单个观测点的处理结果。"""

    index: int
    original: int = Field(description="原始观测序列号")
    effective: int = Field(description="修复后生效的序列号（32 位无符号，模 2^32 回绕）")
    action: Literal["keep", "replace"] = Field(description="keep=保留原值，replace=替换为上一修复后值加 1")
    reason: str


class RepairPlanSummary(BaseModel):
    total: int
    replaced: int = Field(description="被替换的观测点数量")


class RepairPlanOut(BaseModel):
    id: int
    zone: str
    created_at: str
    items: list[RepairPlanItem]
    summary: RepairPlanSummary


class NextSerialOut(BaseModel):
    current: int
    next: int
    date: str = Field(description="建议所基于的日期（UTC，YYYYMMDD）")
    note: str


class SerialReservationCreate(BaseModel):
    """申请为区域原子预留下一个 SOA serial。

    request_id 为幂等键：同一区域同一 request_id 的重试返回首次结果，
    不会再次推进序列号。
    """

    current: Serial
    request_id: str = Field(
        min_length=1,
        max_length=128,
        description="幂等键，由调用方生成（如 UUID）",
        examples=["9f1c2a44-6d6b-4c7e-9f6f-2c0d1f2a7c01"],
    )
    today: date | None = Field(
        default=None,
        description="预留所基于的日期（YYYY-MM-DD），缺省为服务器 UTC 当日",
    )

    @field_validator("request_id")
    @classmethod
    def _check_request_id(cls, value: str) -> str:
        request_id = value.strip()
        if not request_id:
            raise ValueError("request_id 不能为空")
        return request_id


class SerialReservationOut(BaseModel):
    id: int
    zone: str
    request_id: str
    current: int = Field(description="申请时客户端声明的当前 serial")
    serial: int = Field(description="服务端预留的下一 serial")
    date: str = Field(description="预留所基于的日期（UTC，YYYYMMDD）")
    created_at: str
