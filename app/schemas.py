"""请求/响应模型与输入校验。"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from .rfc1982 import MAX_SERIAL

Serial = Annotated[int, Field(ge=0, le=MAX_SERIAL, description="32 位无符号序列号")]

# DNS 区域名：标签由字母/数字/连字符组成，允许末尾根点
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_ZONE_RE = re.compile(rf"^(?:{_LABEL}\.)+{_LABEL}?\.?$|^{_LABEL}\.?$")


def validate_zone(value: str) -> str:
    """去除首尾空白并校验 DNS 区域名，非法时抛 ValueError。"""
    zone = value.strip()
    if not zone or not _ZONE_RE.match(zone):
        raise ValueError("非法的 DNS 区域名")
    return zone


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
        return validate_zone(value)


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
    """序列号预留请求：当前 serial + 幂等键。"""

    current: Serial = Field(
        description="调用方已持有的最新 serial（区域首次预留时为其当前发布值）"
    )
    request_id: str = Field(
        min_length=1,
        max_length=255,
        description="幂等键：同一区域内相同 request_id 的重试返回首次预留结果",
        examples=["deploy-20260919-001"],
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
    current: int = Field(description="首次请求提交的 current 值")
    serial: int = Field(description="服务端原子分配并持久化的下一 serial")
    date: str = Field(description="分配所基于的日期（UTC，YYYYMMDD）")
    created_at: str
    replayed: bool = Field(
        default=False, description="true 表示本次为幂等重试，返回的是首次预留结果"
    )
