# 前端浮层规则

所有遮罩 / overlay 必须由状态驱动显隐（display / visibility / pointer-events）。
禁止常驻透明层依赖 z-index 压制。每次新增浮层后，必须用元素拾取器验证其关闭状态不拦截页面点击。
