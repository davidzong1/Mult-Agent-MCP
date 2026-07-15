"""
Multi-Agent MCP — TUI 子模块
============================

team_manger.py 的 TUI 组件按职责划分的模块边界（供后续渐进拆分）。

当前状态（task3 最小拆分）:
  - 所有 TUI 类仍驻留在 team_manger.py 中以保持入口稳定
  - 公共工具函数已抽取至 common/ (config, data_layer, tmux_utils, mcp_config, mcp_daemon)
  - 后续可渐进将各 TUI 类迁移至此目录:
      components.py  → MessageBox, ConfirmBox, FormField
      dialogs.py     → McpStatusDialog, AgentMcpConfigDialog, CreateTeamDialog, AddMemberDialog, EditMemberDialog
      screens.py     → MainScreen, TeamDetailScreen
      app.py         → TeamManagerApp

依赖关系:
  tui/*  → common/*  (纯单向依赖，无循环导入)
  team_manger.py → tui/* (入口包装)
"""
