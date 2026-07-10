# FDAgent 领域语言（CONTEXT）

本文件是 FDAgent 的**术语表**——只定义领域词汇的规范叫法，不含实现细节。
当前聚焦 C 端用例"这品牌 / 这公司在 FDA 安不安全"的核心身份模型。

## 公司身份：三层模型

**Recalling Firm**（召回主体）:
FDA 记录里"发起这次召回的公司"，对应 `drug_enforcement.recalling_firm` 列。是 FDA 数据中**唯一存在**的公司身份，也是全系统的事实根据。同一实体常有多种原始写法（大小写 / 标点 / 组织单元差异）。**粒度 = 法人级、按法域区分**：美国法人形式（`Inc`/`Corp`/`LLC`/`LP`）视作同一 firm 的噪声写法归并；外国法人形式（`Ltd`/`Limited`/`GmbH`/`Pvt`/`S.A.`…）与美国形式**分属不同 firm**（如 `Sun … Inc`(美) vs `Sun … Ltd`(印)，跨法人合并交给 Parent Group）。
_Avoid_: 公司、firm 名（裸写法）、厂商

**Parent Group**（母公司集团）:
一个 Recalling Firm 向上归并到的企业集团，**包含被收购的子公司**（例：`Hospira Inc.` → `Pfizer`）。**跨法人合并发生在这一层**（例：`Sun Pharma Inc`（美）+ `Sun Pharma Ltd`（印）→ 同一 Parent Group）。FDA 数据里没有这一层，靠外部知识建立，且会随并购 / 拆分**随时间变化**。是 C 端"这家公司安不安全"回答的**默认聚合单位**。
_Avoid_: 母公司（泛指）、owner、集团（裸用）

**Brand / Product**（品牌 / 产品）:
用户嘴里问的那个消费级名字（例：`Tylenol`）。**不是** FDA 结构化字段，只可能出现在 `product_description` 自由文本里；是解析链的**入口**，需先解析到一个或多个 Recalling Firm。
_Avoid_: 产品名（当作身份用）、drug name

## 出处与可核验

**Provenance Tier**（出处档）:
贴在每条事实 / 推断上的"来源等级"标签，取值三档之一——`fda_fact`（FDA 白纸黑字）、`inferred_external_or_llm`（外部知识 / LLM 推断，如 Hospira→Pfizer）、`unknown`（查无，如实说"FDA 未找到"，绝不编造）。用来让用户分清"铁事实"与"我们的推断"。
_Avoid_: 置信度（只指数字）、来源（泛用）

**Evidence**（证据下钻）:
任何汇总数字 / 结论背后都能展开成支撑它的原始召回记录，每条带 FDA 原始 `recalling_firm` 写法 + `recall_number`，供用户自行到 FDA 核对。全系统铁律："数字来自 FDA，可审计"。
_Avoid_: 明细（泛用）、结果列表

## 答案输出

**Recall Profile**（召回画像）:
C 端"这品牌 / 公司安不安全"的回答形态：**两块式**——① **FDA 事实块**（firm 级原样记录，100% `fda_fact`）为锚 + ② **推断块**（加上推断属于该母公司的子公司，如 `Hospira`→`Pfizer`，整块打 `[inferred]` + 出处），两块用分割线隔开。每块给严重度（Class I/II/III）、状态（Ongoing / Terminated）、近期召回，加一句**有边界的解读**，全部可下钻到 `recall_number`。**绝不输出"安全 / 不安全"判决或风险评分**（FDA 召回数据不测量公司安全度；一条召回 = 问题被发现并纠正；零召回 ≠ 安全）。
_Avoid_: 安全评级、safe / unsafe 判定、风险分（risk score）、"这家公司安全"
