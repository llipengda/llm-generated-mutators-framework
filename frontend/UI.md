# 工业测试平台 UI 风格规范（Tailwind CSS）

## 1. 设计目标

本规范用于复刻典型工业软件、测试平台、汽车电子工具链（CANoe/CANalyzer 风格）的桌面应用界面。

设计原则：

* 功能优先于视觉
* 高信息密度
* 长时间使用不疲劳
* 专业工程工具风格
* 减少页面切换
* 配置与结果同屏展示
* 专家用户优先

---

# 2. 风格关键词

```text
Industrial
Engineering
Workbench
Desktop Application
Configuration Driven
High Density
Blue White Gray
```

参考产品：

* Vector CANoe
* Vector CANalyzer
* Wireshark
* MATLAB Desktop
* Jenkins
* Siemens Engineering Tools
* 工业组态软件
* 网络设备管理平台

---

# 3. 整体布局规范

## 标准工作台布局

```text
┌──────────────────────────────────────────┐
│ Ribbon Toolbar                           │
├────────┬───────────────────┬─────────────┤
│        │                   │             │
│ Module │ Main Workspace    │ Properties  │
│ Tree   │                   │ Panel       │
│        │                   │             │
├────────┴───────────────────┴─────────────┤
│ Result Table                             │
└──────────────────────────────────────────┘
```

---

## 尺寸建议

```css
Ribbon Height:      72px~88px

Left Sidebar:       240px~280px

Right Property:     280px~320px

Bottom Result:      200px~280px

Min Content Width:  1200px
```

---

# 4. 色彩系统

## 主色

```css
Primary:
#1976D2

Primary Hover:
#1565C0

Primary Light:
#42A5F5
```

---

## 背景

```css
Page:
#F5F6F7

Panel:
#FFFFFF

Secondary Panel:
#FAFAFA
```

---

## 边框

```css
Border:
#E0E0E0

Border Light:
#EEEEEE
```

---

## 文字

```css
Title:
#333333

Body:
#444444

Secondary:
#666666

Disabled:
#999999
```

---

# 5. Tailwind 配置

## tailwind.config.js

```js
export default {
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: "#1976D2",
          hover: "#1565C0",
          light: "#42A5F5",
        },

        panel: "#FFFFFF",

        page: "#F5F6F7",

        border: "#E0E0E0",

        text: {
          primary: "#333333",
          secondary: "#666666",
        }
      }
    }
  }
}
```

---

# 6. 字体规范

推荐：

```css
font-family:
"Microsoft YaHei",
"Segoe UI",
sans-serif;
```

---

字号：

| 类型   | 大小   |
| ---- | ---- |
| 页面标题 | 16px |
| 分组标题 | 14px |
| 正文   | 13px |
| 标签   | 12px |
| 表格   | 12px |

---

# 7. Ribbon 工具栏

## 结构

```text
文件
硬件
分析
测试
帮助
```

每个 Tab 下包含：

```text
图标
文字
```

组合。

---

## Tailwind

```html
<header
class="
h-20
bg-primary
text-white
flex
items-center
px-4
"
>
</header>
```

---

## 图标规范

大小：

```css
20px~24px
```

风格：

```text
线性图标
简单填充图标
避免拟物化
```

推荐：

* Heroicons
* Lucide

---

# 8. 左侧导航树

## 特征

* TreeView
* 可折叠
* 多级结构
* 层级不超过3层

示例：

```text
MQTT
├─ TCP
├─ UDP

CAN
├─ CAN
├─ CANFD

Bluetooth
```

---

## Tailwind

```html
<div
class="
w-64
border-r
border-border
bg-white
overflow-auto
"
>
</div>
```

---

## Tree Node

```html
<div
class="
h-8
px-3
flex
items-center
text-[13px]
hover:bg-slate-100
cursor-pointer
"
>
CAN
</div>
```

---

# 9. 主工作区

占据最大空间。

用于：

* 配置说明
* 编辑器
* 图表
* 流程说明

---

## 样式

```html
<div
class="
flex-1
bg-white
overflow-auto
"
>
</div>
```

---

## 内容区域

```html
<div
class="
p-6
leading-7
text-[13px]
"
>
</div>
```

---

# 10. 属性面板

工业软件核心组件。

---

## 布局

```text
名称      [________]

协议      [________]

地址      [________]

端口      [________]
```

---

## 样式

```html
<div
class="
w-80
border-l
border-border
bg-[#fafafa]
"
>
</div>
```

---

## Label

```html
<label
class="
text-xs
text-text-secondary
"
>
</label>
```

---

## Input

```html
<input
class="
h-7
w-full
border
border-border
rounded-sm
px-2
text-xs
bg-white
"
/>
```

特点：

* 小尺寸
* 直角
* 无阴影

---

# 11. 表格设计

表格是核心。

---

## 原则

* 高密度
* 小行高
* 多列
* 支持排序
* 支持筛选

---

## Header

```html
<thead
class="
bg-slate-50
"
>
</thead>
```

---

## Cell

```html
<td
class="
h-8
px-3
text-xs
border-b
border-border
"
>
</td>
```

---

## Hover

```html
<tr
class="
hover:bg-slate-50
"
>
</tr>
```

---

# 12. 分组面板

大量使用 Group Box。

---

示例：

```text
基础配置

网络配置

高级配置
```

---

## Tailwind

```html
<section
class="
border
border-border
bg-white
mb-4
"
>
```

标题：

```html
<div
class="
h-9
px-3
border-b
bg-slate-50
font-medium
text-sm
"
>
网络配置
</div>
```

---

# 13. 交互规范

## Hover

```css
background:
#F3F4F6
```

---

## Selected

```css
background:
#E3F2FD

border-left:
3px solid #1976D2
```

---

## Focus

```css
outline: none;

border-color: #1976D2;
```

---

# 14. 禁止项

不要出现：

```text
❌ 毛玻璃

❌ 大圆角

❌ 卡片阴影

❌ 夸张动画

❌ 渐变背景

❌ 大面积插画

❌ 过度留白
```

避免：

```text
现代互联网产品风格
Apple风格
Dribbble风格
Dashboard SaaS风格
```

---

# 15. 推荐组件体系

```text
Ribbon
TreeView
PropertyGrid
Tabs
Splitter
Table
Modal
Drawer
Form
Toolbar
StatusBar
```

---

# 16. 最终视觉特征

如果看到下面这些特点，说明风格正确：

✓ 蓝白灰配色

✓ 大量表格

✓ 大量表单

✓ 左树右属性

✓ 中间工作区

✓ Ribbon工具栏

✓ 高信息密度

✓ 边框分区

✓ 几乎无阴影

✓ 工程师工具感