import { useState } from 'react'
import { Card, Tabs, Typography } from 'antd'

import { DeptPanel } from './admin/DeptPanel'
import { AuditPanel } from './admin/AuditPanel'
import { EvalPanel } from './admin/EvalPanel'
import { FeatureFlagPanel } from './admin/FeatureFlagPanel'
import { GroupPanel } from './admin/GroupPanel'
import { MetricsPanel } from './admin/MetricsPanel'
import { QuotaPanel } from './admin/QuotaPanel'
import { RolePanel } from './admin/RolePanel'
import { SensitiveWordsPanel } from './admin/SensitiveWordsPanel'
import { UserPanel } from './admin/UserPanel'

type AdminTabKey =
  | 'dept'
  | 'users'
  | 'groups'
  | 'roles'
  | 'audit'
  | 'metrics'
  | 'eval'
  | 'quota'
  | 'sensitive'
  | 'feature_flags'

// 系统管理：组织架构 / 用户 / 用户组 / 角色 / 审计日志 五个标签页
// 面板按激活标签懒挂载，切到对应标签时才请求数据
export function AdminPage() {
  const [activeKey, setActiveKey] = useState<AdminTabKey>('dept')

  return (
    <Card variant="borderless">
      <Typography.Title level={4} style={{ marginTop: 0, marginBottom: 4 }}>
        系统管理
      </Typography.Title>
      <Typography.Text type="secondary">
        管理组织架构、用户、用户组、角色，并查看全租户审计日志；配额、敏感词、灰度开关的变更会实时失效旧缓存。
      </Typography.Text>

      <Tabs
        style={{ marginTop: 8 }}
        activeKey={activeKey}
        onChange={(key) => setActiveKey(key as AdminTabKey)}
        items={[
          { key: 'dept', label: '组织架构', children: activeKey === 'dept' ? <DeptPanel /> : null },
          { key: 'users', label: '用户管理', children: activeKey === 'users' ? <UserPanel /> : null },
          {
            key: 'groups',
            label: '用户组',
            children: activeKey === 'groups' ? <GroupPanel /> : null,
          },
          {
            key: 'roles',
            label: '角色管理',
            children: activeKey === 'roles' ? <RolePanel /> : null,
          },
          {
            key: 'audit',
            label: '审计日志',
            children: activeKey === 'audit' ? <AuditPanel /> : null,
          },
          {
            key: 'metrics',
            label: '系统监控',
            children: activeKey === 'metrics' ? <MetricsPanel /> : null,
          },
          {
            key: 'eval',
            label: '评估门禁',
            children: activeKey === 'eval' ? <EvalPanel /> : null,
          },
          {
            key: 'quota',
            label: '配额管理',
            children: activeKey === 'quota' ? <QuotaPanel /> : null,
          },
          {
            key: 'sensitive',
            label: '敏感词',
            children: activeKey === 'sensitive' ? <SensitiveWordsPanel /> : null,
          },
          {
            key: 'feature_flags',
            label: '灰度开关',
            children: activeKey === 'feature_flags' ? <FeatureFlagPanel /> : null,
          },
        ]}
      />
    </Card>
  )
}
