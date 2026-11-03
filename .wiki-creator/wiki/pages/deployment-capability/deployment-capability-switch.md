---
title: 部署期能力开关
topic: deployment-capability
entity_type: 概念
aliases: [能力开关, capability switch, 部署期开关]
sources: [raw/2026-11-03-架构设计文档.md, raw/2026-11-03-测试报告.md]
schema_version: v1
---
# 部署期能力开关

## 摘要
可选能力（图召回、LLM wiki）只由部署期环境变量决定是否启用，运行期不可切换。默认全部关闭，关闭态与未引入该能力时行为一致。

## 定义
与运行时灰度开关的区别有三点：粒度是整个部署而非某租户或某用户；生效方式是改环境变量并重启（镜像内没有源码挂载时还须重建两侧镜像）；审计面是一条启动横幅而非一张规则表。

私有化交付选择部署期的理由：少一个运行时组件、少一份排障面，且「开了什么能力」是部署合同的一部分，应当可被现场核对。

## 关键论点
- 默认值全部为关闭。关闭态不建连接、不调度任务、不注册代码路径。
- api 与 worker 必须取同一组能力值：抽取在 worker、召回在 api，两侧不一致会表现为「任务全部成功但图永远为空」。
- 开启不是「改个变量就生效」：还要跑一次派生数据补建（见 [[config-change-reprocess-matrix]]）。
- 只由环境变量决定，意味着不接受「运维临时改一下」——这类诉求应转为重新部署。

## 证据 / 来源
- 出自 [[capability-failure-semantics]]；原文见 raw/2026-11-03-架构设计文档.md §7.1 配置项清单
- 出自 [[closed-state-equivalence]]；原文见 raw/2026-11-03-测试报告.md §2 关闭态横幅实测

## 关联
- 基础：[[capability-failure-semantics]]
- 对比：[[cross-role-config-fingerprint]]
- 应用：[[config-change-reprocess-matrix]]
