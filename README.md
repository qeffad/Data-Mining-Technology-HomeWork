# AIS 船数核验与 Agent 排查

《海洋数据挖掘技术》课程实验。实验材料包括可编辑 Word 报告、完整的用户与 AI 主对话，以及用于复核实测数字的脚本、数据与工具回执。

- [实验报告修订版](AIS船数核验与Agent排查_实验报告.docx)
- [对话导出：我与 GPT，30 轮](对话导出.md)
- [逐窗评分](experiment/results/agent_scores.json)
- [备版回放检查](experiment/results/revision_check.json)
- [查询顺序的补充对照](experiment/results/query_order_followup.json)


## 复核实验数字

下列命令在本目录运行。实验脚本只使用 Python 标准库。它核对固定种子和冻结数据，执行确定性 Workflow，再根据已归档的盲测回答与实际工具回执复核 Agent 实测结果。

```text
python experiment/ais_audit.py prepare
python experiment/ais_audit.py fixed
python experiment/ais_audit.py score --trial agent --answers experiment/results/agent_answers.json
python experiment/ais_audit.py revision-check
python experiment/query_order_followup.py
```

- `experiment/data/` 包含模拟观察、独立真值、SHA-256 校验和及严格交接子集。
- `experiment/results/agent_answers.json` 是隔离盲测 Agent 给出的逐案答案。
- `experiment/results/agent_probes.jsonl` 和 `fixed_probes.jsonl` 是两组的逐次查询回执。
- 宽筛查是主盲测条件。严格交接结果是使用同批回执计算的事后离线敏感性分析。Agent 全案回答来自一次盲测批次。脚本复核已保存轨迹。重新召集模型会产生新轨迹。
- 查询顺序的补充对照在原批上比较三个确定性策略，并加做一个与早截断样例预检完全相同的构造反例。结果包括逐窗变化和接纳凭证的查询成本。该对照是事后分析，沿用原盲测归档。
