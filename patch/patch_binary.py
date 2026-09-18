import sys

def patch(bin_path):
    with open(bin_path, "rb") as f:
        data = f.read()

    # 1. 替换硬编码的后端端口 57890 为相对路径（window.location.origin）
    target_db = b'const Db="http://127.0.0.1:57890";'
    repl_db = b'const Db=window.location.origin;/*'
    if target_db in data:
        data = data.replace(target_db, repl_db)
        print("Patched Db endpoint to window.location.origin")
    else:
        print("target_db not found (maybe already patched)")

    with open(bin_path, "wb") as f:
        f.write(data)

if __name__ == "__main__":
    patch(sys.argv[1])
