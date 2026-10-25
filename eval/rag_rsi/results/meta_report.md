# RAG 外层 meta-loop

> 内层调检索配置，外层比较“怎么调”的策略；3 折 meta-validation 选择，test 只审计。

- 数据：134 题（dev 67 / test 67；3 折 meta CV）

| 策略 | 同义词数 | 各折 meta-val | 均值 | meta-score | 选中 |
|---|---:|---|---:|---:|---|
| threshold_only | 0 | 0.8667 / 0.9 / 0.8095 | 0.8587 | 0.8587 | 是 |
| generic_synonyms | 2 | 0.8667 / 0.9 / 0.8095 | 0.8587 | 0.8487 |  |
| domain_synonyms | 20 | 0.8875 / 0.9909 / 0.8571 | 0.9118 | 0.8118 |  |

## 选择结果
- 选中策略：`threshold_only`
- 配置：`{"synonyms": {}, "min_score": 3.0, "top_k": 5}`
- meta-val 均值：0.8587
- test：0.8806（gate=通过）
- 最终配置：`{"synonyms": {}, "min_score": 3.0, "top_k": 5}`

## 边界
- 策略空间是固定的 3 种，不是开放式自我改写；
- 3 折 meta-val 选策略，test 只做最终审计；
- 不自动上线。