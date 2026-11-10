# 财报 PDF 全文解析与证据页检索

本分支实现 PowerContext `source-03-pdf-retrieval`，基于官方 `2026-competition` 分支，保留冻结版本 `9db1753434162c0aa7c9f1189a1722e617880a52` 的祖先历史。提交到 seekdb 仓库时应使用独立 `pdf-retrieval` 分支。

## 实现

公共接口位于 `src/powercontext/sources/pdf_pages.py`。解析器使用 pypdf 读取所有物理页（包括空白页），摘要覆盖完整输入字节。空密码权限 PDF 可读取；无法完整解析时抛出 ValueError，不返回部分页。

`pdf-page` Source Definition 版本为 `1`，捕获页输入并提供标准文本投影。Source 身份由文档 ID、全文摘要和物理页码的结构化编码计算 SHA-256，因此文档修订产生新身份，长标识符也不会超过长度上限。

检索通过公开 scoped Memory 的 `list()` 获取一个当前快照，只选取单页 PDF 来源的活动条目，并直接返回条目已有的精确 citation。采用 BM25 词项评分、轻量词形归一化和通用财务同义词扩展；页相关性与文档标识匹配分开计分。不同页去重，尊重 limit，空问题或非正上限返回空元组。

每次检索建立一次请求内词项索引，时间与内存成本均随当前范围的全文词项数线性增长，适用于本题约千页的数据规模。实现不写旁路缓存，不跨范围或修订复用引用，不访问测试资产、gold、答案或外部服务。大型文档库可进一步采用持久化倒排索引减少重复分词成本。

## 验证

在 Windows、Python 3.12、pypdf 6.19.0 环境完成公开测试：完整的 503 页 PDF 入库、幂等提交、Runtime 重启、5 个原始问题和精确 citation 回查，以及空白页、修订、空密码权限和损坏文件测试，全部 6 项通过。另有 6 项产品回归测试覆盖标识符、密码保护、序列化、去重、limit、快照更新及范围隔离。

Windows 本地环境使用可安装的 builtin 依赖版本，并非官方 Linux 完整锁定环境。分支包含 GitHub Actions 工作流，在 Ubuntu、Python 3.12 和 `competition/requirements.txt` 固定依赖下运行公开验收；远端工作流结果须另行核实。

公开自测不包含质量 gold。这些结果不证明隐藏集的解析 F1 或 Recall@20 已达到门槛，也不等同于官方得分。

官方 Linux 环境复现：

```sh
make -C competition install
make -C competition check-baseline
make -C competition test-pdf
```

正式提交需填写仓库 HTTPS URL、`pdf-retrieval` 分支及该分支完整 40 位 commit SHA。仓库代码推送与赛事平台交卷是独立步骤。
