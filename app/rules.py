"""站点详情页字段抽取规则的版本化存储与快照。

背景：供应商改版后，旧的页面节点可能残留过期数据，继续用旧规则抽取会造成
错误报价。本模块按站点（host:port）维护规则版本：

- 每个站点同一时刻只有一个启用版本；内置默认规则为 v1（启发式解析，兼容旧行为）；
- 发布新规则时**原子启用并持久保存**：先写临时文件再 os.replace 落盘，
  落盘成功后才切换内存状态，任何一步失败都保留原有规则；
- 报价开始时通过 snapshot() 取得不可变快照，**固定本次报价的规则版本**——
  即使采集中途有其他请求发布了新版，进行中的报价也不会混用新旧字段。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# 需要抽取的四个字段（顺序即发布/核对时的固定顺序）
FIELD_ORDER = ("model", "pack_size", "pack_price", "stock_packs")
FIELD_LABELS = {
    "model": "型号",
    "pack_size": "每包片数",
    "pack_price": "含税包价",
    "stock_packs": "可购包数",
}


@dataclass(frozen=True)
class RuleVersion:
    """一个规则版本：四项字段各自的 CSS 选择器。

    strict=True 表示严格模式（发布的规则）：只用给定选择器抽取，缺字段或
    多值冲突都视为解析失败；strict=False 为内置默认规则（启发式 + 正则回退）。
    """

    site: str                 # 站点 host:port；内置默认规则为空串
    version: int              # 版本号，站内递增；1 固定为内置默认规则
    selectors: dict[str, str] # 字段名 -> CSS 选择器
    strict: bool
    source: str               # "builtin" | "published"
    created_at: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "selectors": dict(self.selectors),
            "strict": self.strict,
            "source": self.source,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(site: str, d: dict) -> "RuleVersion":
        return RuleVersion(
            site=site,
            version=int(d["version"]),
            selectors={k: str(v) for k, v in d["selectors"].items()},
            strict=bool(d.get("strict", True)),
            source=str(d.get("source", "published")),
            created_at=str(d.get("created_at", "")),
        )


# 内置默认规则 v1：选择器仅作文档展示，实际解析走 scraper 的启发式 + 正则回退
DEFAULT_RULE = RuleVersion(
    site="",
    version=1,
    selectors={
        "model": ".product-model, .model, [data-field='model']",
        "pack_size": ".pack-size, [data-field='pack-size']",
        "pack_price": ".pack-price, .price, [data-field='price']",
        "stock_packs": ".stock-packs, .stock, [data-field='stock']",
    },
    strict=False,
    source="builtin",
    created_at="",
)


class RulesSnapshot:
    """某一时刻各站点启用规则的不可变快照（报价全程固定使用）。"""

    def __init__(self, active: dict[str, RuleVersion]):
        self._active = dict(active)

    def resolve(self, url: str) -> RuleVersion:
        """返回该 URL 所属站点在快照时刻的启用规则；无发布规则时为内置默认 v1。"""
        return self._active.get(urlparse(url).netloc, DEFAULT_RULE)

    def sites(self) -> dict[str, RuleVersion]:
        return dict(self._active)


class RuleStore:
    """规则存储：内存状态 + JSON 文件持久化，发布时原子启用。"""

    def __init__(self, path: str | os.PathLike):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._sites: dict[str, list[RuleVersion]] = {}  # site -> 版本列表（末位为启用版本）
        if self._path.exists():
            self._sites = self._load()

    def _load(self) -> dict[str, list[RuleVersion]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            sites = {}
            for site, entry in data.get("sites", {}).items():
                versions = [RuleVersion.from_dict(site, v) for v in entry.get("versions", [])]
                if versions:
                    sites[site] = versions
            return sites
        except Exception as exc:
            raise RuntimeError(f"规则存储文件损坏 {self._path}: {exc}") from exc

    def _persist(self, sites: dict[str, list[RuleVersion]]) -> None:
        """原子落盘：写临时文件后 os.replace；失败抛错，调用方保持原状态。"""
        payload = {
            "sites": {
                site: {"versions": [v.to_dict() for v in versions]}
                for site, versions in sites.items()
            }
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def snapshot(self) -> RulesSnapshot:
        """当前各站启用版本的快照；报价开始时取一次，全程不再变化。"""
        with self._lock:
            active = {site: versions[-1] for site, versions in self._sites.items()}
        return RulesSnapshot(active)

    def publish(self, site: str, selectors: dict[str, str]) -> RuleVersion:
        """原子启用新规则并持久保存；落盘失败则保留原有规则并抛错。"""
        missing = [f for f in FIELD_ORDER if not str(selectors.get(f, "")).strip()]
        if missing:
            raise ValueError(f"选择器缺少字段: {'、'.join(missing)}")
        with self._lock:
            versions = self._sites.get(site, [])
            next_version = (versions[-1].version + 1) if versions else 2  # v1 为内置默认
            rv = RuleVersion(
                site=site,
                version=next_version,
                selectors={f: str(selectors[f]).strip() for f in FIELD_ORDER},
                strict=True,
                source="published",
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            new_sites = {**self._sites, site: [*versions, rv]}
            self._persist(new_sites)  # 先落盘：失败则内存状态不变，仍用原规则
            self._sites = new_sites
            return rv
