# Pipeline 并行优化说明

## 概述
将原来的 **9 步串行流程** 优化为 **6 个阶段的混合流程**（部分步骤可并行）。

---

## 优化前（串行执行）

```
耗时：T1 + T2 + T3 + T4 + T5 + T6 + T7 + T8 + T9

Step 1 → Step 2 → Step 3 → Step 4 → Step 5 → Step 6 → Step 7 → Step 8 → Step 9
（一个接一个，总耗时：所有步骤的总和）
```

---

## 优化后（支持并行）

### 新的执行结构

```
第一阶段：Step 1（提取包类型）
    ↓
第二阶段：Step 2（生成 C 结构）
    ↓
第三阶段：[Step 3, Step 4] 并行执行（生成解析器和重组器）
    ↓
第四阶段：Step 5（验证解析器和重组器）
    ↓
第五阶段：[Step 6, Step 8] 并行执行（生成变异器和修复器）
    ↓
第六阶段：[Step 7, Step 9] 并行执行（验证变异器和修复器）
```

## 代码改动详情

### 1. 修改 `base.py`

#### 新增导入
```python
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
```

#### 修改 `__call__()` 方法
- **原逻辑**：简单的 while 循环，依次执行每个步骤
- **新逻辑**：
  - 检查当前步骤是单个步骤还是步骤组
  - 单个步骤：直接执行
  - 步骤组（列表）：使用 `ThreadPoolExecutor` 并行执行

#### 新增 `_execute_parallel_steps()` 方法
```python
def _execute_parallel_steps(self, steps_group):
    """并行执行一组步骤"""
    with ThreadPoolExecutor(max_workers=len(steps_group)) as executor:
        # 提交所有任务
        # 等待所有任务完成
        # 收集结果和处理异常
```

**关键特性：**
- 使用线程池并行执行多个步骤
- 等待所有步骤完成后才进入下一阶段
- 如果任何步骤失败，立即抛出异常
- 提供清晰的进度反馈

### 2. 修改 `aflnet.py` 中的 `steps()` 方法

#### 原格式（9 个单步骤）
```python
steps = [
    ("Step 1", func_1),
    ("Step 2", func_2),
    ... 
    ("Step 9", func_9),
]
```

#### 新格式（支持并行）
```python
steps = [
    ("Step 1", func_1),                          # 单个步骤
    ("Step 2", func_2),                          # 单个步骤
    [("Step 3", func_3), ("Step 4", func_4)],   # 并行步骤组
    ("Step 5", func_5),                          # 单个步骤
    [("Step 6", func_6), ("Step 8", func_8)],   # 并行步骤组
    [("Step 7", func_7), ("Step 9", func_9)],   # 并行步骤组
]
```

**优化理由：**
- Step 3 & Step 4 互不依赖，都只依赖 Step 2 的输出（C 结构定义）
- Step 6 & Step 8 互不依赖，都依赖 Step 1（包类型）和 Step 2（结构定义）
- Step 7 & Step 9 都是验证步骤，互不依赖