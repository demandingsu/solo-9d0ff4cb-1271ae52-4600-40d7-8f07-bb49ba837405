# 滤膜采购组合优化 API

实验室耗材采购场景：给定供应商**目录 URL**、**滤膜型号**和**需求片数**，API 用
`requests` + `BeautifulSoup` 抓取同站分页列表与匹配型号的详情页，读取每种包装的
**每包片数 / 含税包价 / 可购包数**，在库存约束内求**总价最低的整包组合**（不同包装可混购；
同价时优先超量更少的组合），并返回各包装采购包数、实到片数、总价与详情来源。

供应商**改版**后，维护员可通过**规则发布 API** 提交同站多张详情页 URL、人工核准的
四项字段值和对应的四条 CSS 选择器；API 逐页抓取核对，**全部吻合才原子启用并持久保存
新规则**，否则保留该站原有规则。报价开始时**固定规则版本**，采集中途发布新版也不会
混用新旧字段；已启用的规则解析失败时不给确定报价。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

包含三个服务：

| 服务 | 地址 | 说明 |
|---|---|---|
| `api` | http://localhost:8000 | FastAPI 服务：报价 API + 规则发布 API（文档见 `/docs`） |
| `sample-site` | http://localhost:8080 | 离线分页样本站 v1（3 页目录 + 5 个详情页） |
| `sample-site-v2` | http://localhost:8081 | **改版后**的离线样本站（详情页换了新节点） |

发布的规则持久化在命名卷 `rules-data`（容器内 `/data/rules.json`），重启不丢。

调用示例（注意：`catalog_url` 由 API 容器访问，Compose 网络内请用服务名
`http://sample-site:8080/`，而不是 `localhost:8080`）：

```bash
curl -X POST http://localhost:8000/api/v1/quote \
  -H 'Content-Type: application/json' \
  -d '{"catalog_url": "http://sample-site:8080/", "model": "MF-50", "demand": 180}'
```

响应（最优组合：2 包 100 片经济装，总价 ¥450）：

```json
{
  "status": "ok",
  "model": "MF-50",
  "demand": 180,
  "rule_version": 1,
  "rule_source": "builtin",
  "list_pages": ["http://sample-site:8080/", "..."],
  "matched_detail_pages": ["...mf50-50.html", "...mf50-100.html", "...mf50-25.html"],
  "duplicate_detail_links_skipped": 1,
  "solution": {
    "items": [
      {
        "detail_url": "http://sample-site:8080/details/mf50-100.html",
        "title": "MCE 微孔滤膜 MF-50（100片经济装）",
        "model": "MF-50",
        "pack_size": 100,
        "pack_price": "225.00",
        "stock_packs": 10,
        "purchased_packs": 2,
        "delivered_pieces": 200,
        "subtotal_price": "450.00"
      }
    ],
    "total_pieces": 200,
    "demand": 180,
    "overage": 20,
    "total_price": "450.00",
    "currency": "CNY"
  },
  "total_available_pieces": 3750
}
```

## API

### `POST /api/v1/quote`

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `catalog_url` | string | 供应商目录列表页 URL（任意一页均可，自动沿"下一页"翻页） |
| `model` | string | 滤膜型号，如 `MF-50`（忽略大小写、空格与连字符的包含匹配） |
| `demand` | int ≥ 1 | 需求片数 |

响应 `status` 语义：

| status | 含义 | 是否给出确定最低价 |
|---|---|---|
| `ok` | 已找到库存内总价最低的整包组合 | ✅ `solution` |
| `insufficient_stock` | 可购总量 < 需求（附 `total_available_pieces`） | ❌ |
| `scrape_failed` | 匹配详情页抓取/解析失败（附 `failed_detail_pages`） | ❌ |
| `no_matching_products` | 目录中无匹配型号（若有页面未能核对会附 `unverified_failed_pages`） | ❌ |
| `catalog_unreachable` | 目录列表页抓取失败 | ❌ |
| `demand_too_large` | 需求超出求解器安全上限 | ❌ |

`solution.items[]` 给出每种包装的**采购包数、实到片数、含税包价、小计与详情来源 URL**；
`solution` 另含**总实到片数、超量（overage）、总价**。

响应中的 `rule_version` / `rule_source` 是本次报价**开始时固定**的详情页抽取规则版本
（`builtin` = 内置默认 v1，`published` = 已发布的规则）：整次抓取只使用该版本，
即使采集中途有新规则发布，也不会混用新旧字段。

### `POST /api/v1/rules/publish`

供应商改版后发布新的详情页字段抽取规则。提交**同站**多张详情页 URL、每页人工核准的
**型号 / 每包片数 / 含税包价 / 可购包数**，以及对应的**四条 CSS 选择器**；API 逐页
抓取并用候选规则核对：

- 任一页面**抓取失败**、任一字段**缺值**（选择器未命中或值无法解析）、
  **多值冲突**（选择器命中多个不同的值）或**与核准值不符** → 保留该站原有规则；
- **全部页面全部字段吻合** → 原子启用新规则并持久保存（先落盘后切换，失败不留半成品）。

请求体：

```json
{
  "pages": [
    {
      "url": "http://sample-site-v2:8080/details/mf50-100.html",
      "expected": {"model": "MF-50", "pack_size": 100, "pack_price": "235.00", "stock_packs": 12}
    },
    {
      "url": "http://sample-site-v2:8080/details/mf50-50.html",
      "expected": {"model": "MF-50", "pack_size": 50, "pack_price": "125.00", "stock_packs": 28}
    },
    {
      "url": "http://sample-site-v2:8080/details/mf50-25.html",
      "expected": {"model": "MF-50", "pack_size": 25, "pack_price": "72.00", "stock_packs": 40}
    }
  ],
  "selectors": {
    "model": "span.sku",
    "pack_size": "span.pack-spec",
    "pack_price": "span.price-now",
    "stock_packs": "span.stock-left"
  }
}
```

- `pack_price` 单位为元（字符串或数字均可）；型号按归一化包含匹配核对，其余字段精确相等；
- 所有 URL 必须同站（同 host），否则 400；选择器语法非法同样 400。

响应（`status: "published" | "rejected"`）含逐页核对结果 `pages[]`：每页 `ok`、
逐字段的 `selector` / `expected` / `actual` / `error`
（`fetch_failed` / `missing` / `conflict` / `mismatch`），以及是否启用
（`activated`）与当前生效的 `rule_version`。

发布成功后，对该站的报价立即使用新版本：

```bash
curl -X POST http://localhost:8000/api/v1/quote \
  -H 'Content-Type: application/json' \
  -d '{"catalog_url": "http://sample-site-v2:8080/", "model": "MF-50", "demand": 180}'
# → rule_version: 2，2 包 100 片装，总价 ¥470.00（改版后新价 ¥235/包）
```

### `GET /api/v1/rules`

查看内置默认规则与各站点当前启用的规则版本、选择器与发布时间。

## 规则与实现要点

- **同站分页**：从 `catalog_url` 出发，识别 `rel="next"` / `class="next"` / 分页区
  "下一页"链接，仅跟随与目录同站点（同 host）的链接； visited 集合防环，上限 50 页。
- **跨页去重**：同一详情 URL 在多个列表页重复出现只计一次
  （`duplicate_detail_links_skipped` 计数）。
- **型号匹配**：先按列表文案匹配；一个都匹配不上时退回逐个抓取详情页核对型号
  （此时若有页面抓取失败，因无法确认是否漏掉更优匹配，同样不给确定最低价）。
- **详情解析（规则版本化，`app/rules.py` + `app/scraper.py`）**：
  - 规则按站点（host）版本化，内置默认 v1 为启发式：优先 CSS 选择器
    （`.product-model` / `.pack-size` / `.pack-price` / `.stock-packs`），
    回退到中文标签正则（`型号：`、`每包片数：`、`含税包价：`、`可购包数：`）；
  - 已发布的规则为**严格模式**：只用其四条选择器抽取，**不做启发式回退**
    （改版后旧节点可能残留过期数据，回退会造成错误报价）；缺字段或多值冲突
    即视为该详情解析失败；
  - 价格以"分"为整数运算，避免浮点误差。
- **版本固定**：报价开始时取一次规则快照，整次抓取（列表 + 全部详情页）只用它；
  发布新规则是原子操作（临时文件 + `os.replace` 落盘后再切内存状态），
  进行中的报价不受影响。
- **组合优化**（`app/optimizer.py`）：有界背包 → 二进制拆分转 0/1 背包，对
  "恰好 j 片"做最小费用 DP，再按 **(总价, 总片数)** 字典序取最优——即总价最低，
  同价时超量更少。可证最优解总片数 < 需求 + 最大包装片数，DP 状态有界。
- **不给确定最低价的情形**：库存总量不足；匹配详情页抓取或解析失败
  （含已启用规则解析失败）。

## 样本站说明

### `sample_site/`（v1）

- 3 页分页目录，`index.html → page2.html → page3.html`；
- `MF-50` 三种包装：50片/¥120（30包）、100片/¥225（10包）、25片/¥68（50包），
  可购总量 3750 片；
- `mf50-50.html` 在第 1、2 页**重复出现**（验证跨页去重）；
- `BROKEN-1` 的详情链接故意 404（验证"匹配详情抓取失败"分支）；
- 另有 `NY-66`、`CA-22` 两个其他型号商品。

试几组：`demand=180` → 2×100片 ¥450；`demand=175` → 100+50+25 各一包 ¥413（零超量）；
`demand=1000` → 买光 10 包 100 片装 ¥2250；`demand=4000` → `insufficient_stock`。

### `sample_site_v2/`（改版后）

- 同样的 3 页目录与 5 个商品，但**详情页换了新节点**：
  `span.sku` / `span.pack-spec` / `span.price-now` / `span.stock-left`，
  且价格库存已更新（如 MF-50 100片装 ¥235.00 / 12 包）；
- `mf50-100.html` 里保留了**隐藏的遗留旧节点**（`.pack-price` = 改版前 ¥225.00），
  演示旧规则误抓旧节点造成错误报价的风险；
- 用默认规则对 v2 站报价会因部分页面缺字段而 `scrape_failed`（不给确定报价）；
  按上文示例发布新规则后即可正常报价（MF-50/180 → 2×100片 ¥470）。

## 本地开发与测试

```bash
pip install -r app/requirements.txt
python -m unittest discover -s tests -v   # 优化器 + 规则存储单元测试
python tests/e2e_check.py                 # 端到端检查（本地起样本站，走完整 API 链路）
```

规则存储默认写在 `./data/rules.json`（可用环境变量 `RULES_STORE_PATH` 覆盖）；
端到端检查使用独立的临时存储，不会污染本地文件。

## 目录结构

```
app/
  main.py        FastAPI 入口：报价 API + 规则发布 API
  scraper.py     分页抓取 + 详情解析（启发式 / 严格规则两种模式）
  rules.py       规则版本化存储：快照固定、原子启用、持久化
  optimizer.py   库存约束下的最低总价整包组合（DP）
  requirements.txt / Dockerfile
sample_site/     离线分页样本站 v1（静态 HTML）
sample_site_v2/  改版后的离线样本站（详情页新节点）
tests/           单元测试 + 端到端检查
docker-compose.yml
```
