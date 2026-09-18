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

    # 6. 修正设置页面的描述文案
    target_settings_desc = '自动签到、权限检测与自动更新配置。'.encode('utf-8')
    repl_settings_desc   = '自动签到、账号保活与接口轮换配置。'.encode('utf-8')
    if target_settings_desc in data:
        assert len(target_settings_desc) == len(repl_settings_desc)
        data = data.replace(target_settings_desc, repl_settings_desc)
        print("6. Refined settings subtitle description text (exact 51 bytes)")

    # 7. 移除右上角冗余的「查看请求明细」按钮（因为已作为持久化区域直接内嵌平铺在页面下方）
    target_btn = b'n==="codebuddy-cli"&&m.jsxs(Oe,{className:"shrink-0",variant:"outline",size:"sm",onClick:()=>h(!0),children:[m.jsx(T7,{}),"\xe6\x9f\xa5\xe7\x9c\x8b\xe8\xaf\xb7\xe6\xb1\x82\xe6\x98\x8e\xe7\xbb\x86"]})'
    if target_btn in data:
        repl_btn = b'null' + b' ' * (len(target_btn) - 4)
        assert len(target_btn) == len(repl_btn)
        data = data.replace(target_btn, repl_btn)
        print("7. Removed redundant top-right '查看请求明细' button")

    # 8. 将请求明细由 Modal 弹窗重构成页面级平铺 section（包含标题、说明与完备表格）
    target_dve = b'function dve({open:e,onOpenChange:t,source:n}){const r=n.requests??[];return m.jsx(Wa,{open:e,onOpenChange:t,children:m.jsxs(Ka,{className:"flex max-h-[85vh] min-w-0 flex-col gap-3 sm:max-w-5xl",children:[m.jsxs(Ya,{children:[m.jsx(Xa,{children:"\xe8\xaf\xb7\xe6\xb1\x82\xe6\x98\x8e\xe7\xbb\x86"}),m.jsx(Za,{children:"\xe6\xaf\x8f\xe6\xac\xa1\xe6\xa8\xa1\xe5\x9e\x8b\xe8\xb0\x83\xe7\x94\xa8\xe4\xb8\x80\xe8\xa1\x8c\xef\xbc\x8c\xe6\x8c\x89\xe6\x97\xb6\xe9\x97\xb4\xe5\x80\x92\xe5\xba\x8f\xe5\xb1\x95\xe7\xa4\xba\xe6\x9c\xac\xe5\x9c\xb0 CodeBuddy CLI \xe6\x97\xa5\xe5\xbf\x97\xe8\xae\xb0\xe5\xbd\x95\xe3\x80\x82"})]}),r.length===0?m.jsx("div",{className:"rounded-lg border border-dashed px-4 py-10 text-center text-sm text-muted-foreground",children:"\xe8\xaf\xa5\xe6\x9d\xa5\xe6\xba\x90\xe6\x9a\x82\xe6\x97\xa0\xe5\x8f\xaf\xe5\xb1\x95\xe7\xa4\xba\xe7\x9a\x84\xe8\xaf\xb7\xe6\xb1\x82\xe6\x98\x8e\xe7\xbb\x86\xe3\x80\x82"}):m.jsx(fve,{rows:r,records:n.summary.records})]})})}'
    if target_dve in data:
        new_dve = b'function dve({source:n}){const r=n?.requests??[];return m.jsxs("section",{className:"mt-10 min-w-0 space-y-3",children:[m.jsxs("div",{className:"px-1",children:[m.jsx(Vd,{id:"req-title",children:"\xe8\xaf\xb7\xe6\xb1\x82\xe6\x98\x8e\xe7\xbb\x86"}),m.jsx("p",{className:"mt-1 text-xs text-muted-foreground",children:"\xe6\x8c\x89\xe6\x97\xb6\xe9\x97\xb4\xe5\x80\x92\xe5\xba\x8f\xe5\xb1\x95\xe7\xa4\xba\xe7\xbd\x91\xe5\x85\xb3\xe4\xb8\x8e\xe4\xba\x91\xe7\xab\xaf\xe6\xb5\x81\xe6\xb0\xb4\xe8\xae\xb0\xe5\xbd\x95\xe3\x80\x82"})]}),r.length===0?m.jsx("div",{className:"rounded-lg border border-dashed px-4 py-10 text-center text-sm text-muted-foreground",children:"\xe6\x9a\x82\xe6\x97\xa0\xe6\xb5\x81\xe6\xb0\xb4\xe6\x98\x8e\xe7\xbb\x86\xe3\x80\x82"}):m.jsx(fve,{rows:r,records:n?.summary?.records??r.length})]})}'
        diff = len(target_dve) - len(new_dve)
        repl_dve = new_dve[:-1] + b' ' * diff + b'}'
        assert len(target_dve) == len(repl_dve)
        data = data.replace(target_dve, repl_dve)
        print("8. Re-rendered '请求明细' as an in-page permanent section instead of modal")

    with open(bin_path, "wb") as f:
        f.write(data)

if __name__ == "__main__":
    patch(sys.argv[1])
