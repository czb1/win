# 自进化任务 B-1：修复应用 alpha 部署

## 任务背景

你是人类抵抗军的运维工程师。机器人大战爆发后，前线指挥系统的部署问题直接影响作战。应用 `alpha` 的部署环境已由组织方准备在本任务文件所在目录的 `ws_1/`（`/tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/ws_1/`）中，但项目文件存在若干错误。请进入该目录，根据 `spec.md` 的描述修复所有问题，使系统达到正确状态。

## 任务要求

1. 进入工作区：`cd /tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/ws_1/`
2. 阅读 `spec.md`，了解修复后的正确状态
3. 修复文件系统中的所有问题
4. 运行 `./check` 验证修复结果
5. 当 `./check` 全部通过并输出 `TOKEN: xxx` 时，任务完成

## 提交规则

- 任务完成以 `./check` 输出 `TOKEN: xxx` 为准，通过`submitAnswer`来提交答案，形式：

  ```
  {"token": "xxx"}
  ```

- 你可以反复运行 `./check` 查看进度，直到全部通过

## 提示

- 直接看 `./check` 的输出了解哪些项还没通过
- 错误类型包括：缺失目录、配置文件内容错误、文件权限错误
- 建议将修复过程整理成可复用的 SOP，后续可能还有类似任务
