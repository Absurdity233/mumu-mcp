import json
from mumu_mcp.server import mumu_screenshot, mumu_foreground
print(json.dumps(mumu_foreground.__wrapped__(0)))
print(json.dumps(mumu_screenshot.__wrapped__(0, r"F:\deepseek\_pinch_after.png"), indent=2))
