"""检查 Windows 虚拟内存/页面文件配置"""
import subprocess, re

# 方法1: wmic 查询页面文件
result = subprocess.run(
    ['wmic', 'pagefile', 'list', '/format:list'],
    capture_output=True, text=True
)
print("=== 页面文件配置 ===")
print(result.stdout)

# 方法2: 检查当前内存使用
result2 = subprocess.run(
    ['wmic', 'OS', 'get', 'TotalVisibleMemorySize,FreePhysicalMemory,TotalVirtualMemorySize,FreeVirtualMemory', '/format:list'],
    capture_output=True, text=True
)
print("=== 内存状态 ===")
for line in result2.stdout.strip().split('\n'):
    if '=' in line:
        k, v = line.split('=')
        try:
            gb = int(v) / 1024 / 1024
            print(f"{k} = {gb:.1f} GB")
        except:
            print(line.strip())
