# Shopify 首页内嵌铭牌匹配组件安装说明

## 文件

已经生成 Shopify Section：

`shopify_sections/nameplate-gasket-match.liquid`

## 放到 Shopify 哪里

1. Shopify 后台进入 `Online Store`
2. 打开 `Themes`
3. 当前主题点 `...`
4. 点 `Edit code`
5. 左侧打开 `Sections`
6. 点 `Add a new section`
7. 名字填：

```text
nameplate-gasket-match
```

8. 把 `nameplate-gasket-match.liquid` 的内容全部粘进去
9. 保存

## 添加到首页

1. 回到 `Online Store > Themes`
2. 点 `Customize`
3. 进入首页模板
4. 点 `Add section`
5. 选择 `Nameplate gasket match`
6. 拖到首页第一屏

## Shopify 商品设置

建议只建 1 个商品：

```text
Custom Refrigerator Door Gasket
```

然后建 4 个 Variant：

```text
Up to 98 in perimeter   $45
Up to 117 in perimeter  $68
Up to 146 in perimeter  $90
Up to 190 in perimeter  $120
```

把 4 个 Variant ID 填到 Section 设置：

```text
Variant ID: gasket under 98 in / $45
Variant ID: gasket under 117 in / $68
Variant ID: gasket under 146 in / $90
Variant ID: gasket under 190 in / $120
```

组件会根据：

```text
perimeter = (width + height) * 2
```

自动决定价格档。

## API 地址

第一版可以先不填 `Match API endpoint`，组件会用 demo 数据测试交互。

等我们的匹配 API 部署后，再填：

```text
https://your-api-domain.com/api/nameplate-match
```

API 返回格式建议：

```json
{
  "brand": "Continental",
  "model": "D2R",
  "product_image_url": "https://...",
  "confidence_score": 86,
  "doors": [
    {
      "door_position": "Main door",
      "width_in": 29.25,
      "height_in": 67,
      "dimensions_text": "29.25 x 67"
    }
  ]
}
```

## Checkout 逻辑

客户可以勾选一条或多条门封条。

组件会自动生成 Shopify cart 链接：

```text
/cart/VARIANT_ID:quantity,VARIANT_ID:quantity
```

例如：

```text
/cart/123456789:2,987654321:1
```

Shopify 会合并结算并发送订单确认邮件。
