# Gasket Match Center

Customer-facing refrigerator door gasket matching site and upload workflow.

## Core Frontend Flow（唯一对外入口）

前台只保留一条主链路，用户操作顺序固定为：

1. 上传图片  
2. 识别  
3. 匹配  
4. 展示匹配结果

对应入口：`nameplate_web_app.py`（前端服务器）  
启动方式：`run_nameplate_web_app.ps1`（或直接运行 `python nameplate_web_app.py`）

---

## Script Classification（脚本分类）

### 保留并可按需手动执行

- `run_market_discovery.ps1`  
  启动品牌/型号市场发现与扫描流程

- `run_product_image_search.ps1`  
  启动产品图片补充流程

- `run_gasket_enrichment.ps1`  
  启动门封条资料补充流程

- `run_product_metadata_enrichment.ps1`  
  启动主表元数据补充流程

### 归档（不作为主链路）

- `run_crawler.ps1`  
  旧模型入库脚本（已归档）。保留引用版本在 `archived/run_crawler.ps1`，不再作为日常入口

### 保留为应急工具（非主链路）

- `run_nameplate_to_work_order.ps1`  
  紧急处理脚本，默认不纳入前台标准流程

---

## Deployment（Render）

- Runtime: `Python 3`
- Build Command: `pip install -r requirements.txt`
- Start Command: `python nameplate_web_app.py`

## Environment variables（环境变量）

必填：

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

可选：

- `GOOGLE_API_KEY`
- `GOOGLE_CSE_ID`

> 不要提交 `.env` 文件和私钥到仓库。

---

## Run locally（本地运行）

```powershell
python nameplate_web_app.py
```

然后打开：

```text
http://127.0.0.1:8000/
```
