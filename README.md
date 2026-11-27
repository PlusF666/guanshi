# 观市

沪深主板行情研究与模拟交易终端。支持分时图、VWAP、个股决策复核、自定义本金和独立策略对照。

## Windows 使用

在 GitHub Releases 下载 Windows 程序，双击运行。已有账户可退出旧终端后，将新版程序放到原数据库所在目录再打开。

## 从源码运行

```powershell
python -m pip install -r requirements.txt
python terminal.py
```

行情通过网络读取。账户、成交和复盘记录保存在程序目录的 SQLite 数据库中。
