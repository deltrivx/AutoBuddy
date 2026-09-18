import sys

def patch(bin_path):
    with open(bin_path, "rb") as f:
        data = f.read()

    # 1. 替换硬编码的后端端口 57890 为相对路径（window.location.origin）
    target_db = b'const Db="http://127.0.0.1:57890";'
    repl_db   = b'const Db=window.location.origin;  '
    if target_db in data:
        data = data.replace(target_db, repl_db)
        print("1. Patched Db endpoint to window.location.origin")

    # 2. 彻底移除前端「导入本机账号」按钮代码 (以同等长度的 null 替换)
    target_import = b'm.jsx(Dt,{children:m.jsxs(Oe,{className:"h-10 px-4",onClick:pl,disabled:M,variant:"outline",children:[M?m.jsx(_t,{className:"animate-spin"}):m.jsx(nI,{}),t==="ai"?"\xe5\xaf\xbc\xe5\x85\xa5\xe6\x9c\xac\xe6\x9c\xba\xe5\x9b\xbd\xe9\x99\x85\xe7\x89\x88\xe8\xb4\xa6\xe5\x8f\xb7":"\xe5\xaf\xbc\xe5\x85\xa5\xe6\x9c\xac\xe6\x9c\xba\xe8\xb4\xa6\xe5\x8f\xb7"]})})'
    if target_import in data:
        repl_import = b'null' + b' ' * (len(target_import) - 4)
        assert len(target_import) == len(repl_import)
        data = data.replace(target_import, repl_import)
        print("2. Completely stripped 'import-local' button from binary JSX")

    # 3. 彻底移除 Token 统计上方的无效多余分组 Tab 栏 (YO/XO)
    target_tabs = b'm.jsx(YO,{className:"mb-8 min-w-0 gap-0",value:n,onValueChange:x=>{r6(x)&&(e&&!e.sources.some(w=>w.source===x)||r(x))},children:m.jsxs(XO,{className:"h-auto max-w-full flex-wrap","aria-label":"Token \xe6\x95\xb0\xe6\x8d\xae\xe6\x9d\xa5\xe6\xba\x90",children:[m.jsx(Ds,{className:"max-w-full whitespace-normal",value:"workbuddy",disabled:!!(e&&!e.sources.some(x=>x.source==="workbuddy")),children:"WorkBuddy"}),m.jsx(Ds,{className:"max-w-full whitespace-normal",value:"workbuddy-ai",disabled:!!(e&&!e.sources.some(x=>x.source==="workbuddy-ai")),children:"WorkBuddy \xe5\x9b\xbd\xe9\x99\x85\xe7\x89\x88"}),m.jsx(Ds,{className:"max-w-full whitespace-normal",value:"codebuddy-cli",disabled:!!(e&&!e.sources.some(x=>x.source==="codebuddy-cli")),children:"CodeBuddy CLI"}),m.jsx(Ds,{className:"max-w-full whitespace-normal",value:"codebuddy-ide",disabled:!!(e&&!e.sources.some(x=>x.source==="codebuddy-ide")),children:"CodeBuddy IDE"})]})})'
    if target_tabs in data:
        repl_tabs = b'null' + b' ' * (len(target_tabs) - 4)
        assert len(target_tabs) == len(repl_tabs)
        data = data.replace(target_tabs, repl_tabs)
        print("3. Completely stripped 4-tab source buttons from token-stats page")

    # 4. 彻底移除 Token 统计页面中冗余的「Token 总览」小标题提示
    target_overview_title = b'm.jsx(Vd,{id:"token-overview-title",children:"Token \xe6\x80\xbb\xe8\xa7\x88"})'
    if target_overview_title in data:
        repl_overview_title = b'null' + b' ' * (len(target_overview_title) - 4)
        assert len(target_overview_title) == len(repl_overview_title)
        data = data.replace(target_overview_title, repl_overview_title)
        print("4. Completely stripped redundant 'Token 总览' title from token-stats page")

    # 5. 彻底移除设置页面中无效的「权限检测」模块 (m.jsx(_ye,{}))
    target_permission = b'm.jsx(_ye,{})'
    if target_permission in data:
        repl_permission = b'null' + b' ' * (len(target_permission) - 4)
        assert len(target_permission) == len(repl_permission)
        data = data.replace(target_permission, repl_permission)
        print("5. Completely stripped '权限检测' module from settings page")

    # 6. 修正设置页面的描述文案，去除容器无关说明
    target_settings_desc = '自动签到、权限检测与自动更新配置。'.encode('utf-8')
    repl_settings_desc   = '自动签到、账号保活与接口轮换配置。'.encode('utf-8')
    if target_settings_desc in data:
        assert len(target_settings_desc) == len(repl_settings_desc)
        data = data.replace(target_settings_desc, repl_settings_desc)
        print("6. Refined settings subtitle description text (exact 51 bytes)")

    with open(bin_path, "wb") as f:
        f.write(data)

if __name__ == "__main__":
    patch(sys.argv[1])
