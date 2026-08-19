# 主屏图标

小朵自己画的。把导出的 PNG 放进这个目录，文件名必须完全对上：

| 文件名 | 尺寸 | 谁在用 |
|---|---|---|
| `nepeta-180.png` | 180×180 | **iOS 主屏图标（唯一必需的）** |
| `nepeta-192.png` | 192×192 | Android / Chrome（可选） |
| `nepeta-512.png` | 512×512 | 启动画面大图（可选） |

只放 180 那个就够了，她只用 iPhone。

## 导出时注意

- **不要自己加圆角。** iOS 会自己切一次，自带圆角会被切两遍，边缘出现
  奇怪的白边。给满幅方图。
- **不要透明背景。** 透明区域在 iOS 上会变成黑色。
- **背景要铺到边。** iOS 切圆角会削掉四角，构图别顶到角上。
- 必须是 PNG。**iOS 的 apple-touch-icon 不支持 SVG** —— 之前主屏上那个
  丑 N 就是因为塞了 SVG，被静默忽略后 iOS 拿站名首字母顶上了。

## 不要引 manifest

`index.html` 的 head 里**永远不要**加 `link rel=manifest`：iOS 一看到它就
退回实心状态栏，`black-translucent` 的沉浸式效果当场作废。

主屏图标不需要 manifest——iOS 只认 `apple-touch-icon`，manifest 里的
`icons` 数组是给 Android / Chrome 看的。

## 文件没放进来会怎样

`/icons/{name}` 路由返回 404，head 里第 14 行那个 SVG 兜底，
行为跟加这套东西之前完全一样。不会因为半成品把图标搞坏。
