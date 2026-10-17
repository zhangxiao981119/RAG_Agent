// 知识治理面板（M7 WU-M-18）—— 四维审查结论一览
// 数据源复用 KB 文档列表接口（自带 KB 级授权校验），前端做状态过滤；
// failed/done 文档可手动重跑审查（POST /documents/{id}/governance/rerun，仅 admin）
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App,
  Button,
  Descriptions,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { ReloadOutlined, SyncOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import dayjs from 'dayjs'

import {
  fetchDocuments,
  fetchKbs,
  rerunGovernance,
  type Document,
  type GovernanceReport,
} from '../../mocks/data'
import { errMsg } from './common'

// 治理状态 → Tag 展示
const STATUS_META: Record<string, { color: string; label: string }> = {
  pending: { color: 'default', label: '待审查' },
  running: { color: 'processing', label: '审查中' },
  done: { color: 'success', label: '已审查' },
  failed: { color: 'error', label: '审查失败' },
}

// 四维 key → 中文名
const DIM_LABEL: Record<string, string> = {
  freshness: '时效性',
  authority: '权威性',
  consistency: '一致性',
  completeness: '完整性',
}

// 分数 → 颜色分档（>=80 健康 / >=60 存疑 / <60 预警）
function scoreColor(score: number): string {
  if (score >= 80) return '#52c41a'
  if (score >= 60) return '#faad14'
  return '#ff4d4f'
}

// 单维分数单元格：null = 未审查显示灰色占位
function ScoreCell({ score }: { score: number | null }) {
  if (score == null) return <Typography.Text type="secondary">—</Typography.Text>
  return <span style={{ color: scoreColor(score), fontWeight: 600 }}>{score}</span>
}

export function GovernancePanel() {
  const { message } = App.useApp()
  const [kbs, setKbs] = useState<{ id: string; name: string }[]>([])
  const [kbId, setKbId] = useState<string>('')
  const [docs, setDocs] = useState<Document[]>([])
  const [statusFilter, setStatusFilter] = useState<string>('all')
  const [loading, setLoading] = useState(false)
  const [rerunningId, setRerunningId] = useState<string | null>(null)

  const loadKbs = useCallback(async () => {
    try {
      const data = await fetchKbs()
      setKbs(data.map((k) => ({ id: k.id, name: k.name })))
      if (data.length > 0) {
        setKbId((prev) => prev || data[0].id)
      }
    } catch (e) {
      message.error(errMsg(e))
    }
  }, [message])

  const loadDocs = useCallback(
    async (targetKb: string) => {
      if (!targetKb) return
      setLoading(true)
      try {
        const data = await fetchDocuments(targetKb)
        setDocs(data)
      } catch (e) {
        message.error(errMsg(e))
      } finally {
        setLoading(false)
      }
    },
    [message],
  )

  useEffect(() => {
    void loadKbs()
  }, [loadKbs])

  useEffect(() => {
    if (kbId) void loadDocs(kbId)
  }, [kbId, loadDocs])

  async function handleRerun(row: Document) {
    setRerunningId(row.id)
    try {
      await rerunGovernance(row.id)
      message.success(`已受理「${row.filename}」的重跑请求，审查将在后台执行`)
      // 重跑受理后本地状态置 pending，等待下次刷新拿真实结论
      setDocs((prev) =>
        prev.map((d) => (d.id === row.id ? { ...d, governance_status: 'pending' } : d)),
      )
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setRerunningId(null)
    }
  }

  // 状态过滤（前端筛：列表接口已按 KB 拉全量）
  const filtered = useMemo(
    () => (statusFilter === 'all' ? docs : docs.filter((d) => d.governance_status === statusFilter)),
    [docs, statusFilter],
  )

  const columns: ColumnsType<Document> = [
    {
      title: '文档',
      dataIndex: 'filename',
      key: 'filename',
      width: 260,
      ellipsis: true,
      render: (v: string, record) => (
        <Space size={6}>
          <span>{v}</span>
          <Tag>{record.ext}</Tag>
        </Space>
      ),
    },
    {
      title: '审查状态',
      dataIndex: 'governance_status',
      key: 'governance_status',
      width: 100,
      render: (v: string) => {
        const meta = STATUS_META[v] ?? { color: 'default', label: v }
        return <Tag color={meta.color}>{meta.label}</Tag>
      },
    },
    {
      title: '时效性',
      key: 'freshness',
      width: 80,
      align: 'center',
      render: (_, r) => <ScoreCell score={r.freshness_score} />,
    },
    {
      title: '权威性',
      key: 'authority',
      width: 80,
      align: 'center',
      render: (_, r) => <ScoreCell score={r.authority_score} />,
    },
    {
      title: '一致性',
      key: 'consistency',
      width: 80,
      align: 'center',
      render: (_, r) => <ScoreCell score={r.consistency_score} />,
    },
    {
      title: '完整性',
      key: 'completeness',
      width: 80,
      align: 'center',
      render: (_, r) => <ScoreCell score={r.completeness_score} />,
    },
    {
      title: '治理权重',
      key: 'weight',
      width: 90,
      align: 'center',
      render: (_, r) => {
        const w = r.governance_report?.governance_weight
        if (r.governance_status !== 'done' || typeof w !== 'number') {
          return <Typography.Text type="secondary">—</Typography.Text>
        }
        const color = w > 0 ? '#52c41a' : w < 0 ? '#ff4d4f' : '#8c8c8c'
        return (
          <span style={{ color, fontWeight: 600 }}>
            {w > 0 ? `+${w}` : w}
          </span>
        )
      },
    },
    {
      title: '审查时间',
      key: 'reviewed_at',
      width: 150,
      render: (_, r) => {
        const t = r.governance_report?.reviewed_at
        return t ? dayjs(t).format('YYYY-MM-DD HH:mm') : '—'
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 90,
      render: (_, record) => {
        const busy = record.governance_status === 'pending' || record.governance_status === 'running'
        const notIndexed = record.status !== 'indexed'
        const disabled = busy || notIndexed
        const tip = notIndexed
          ? '文档未解析完成，无法审查'
          : busy
            ? '审查已排队或进行中'
            : `对「${record.filename}」重新执行四维审查`
        return (
          <Button
            type="link"
            size="small"
            icon={<SyncOutlined />}
            disabled={disabled}
            loading={rerunningId === record.id}
            title={tip}
            onClick={() => void handleRerun(record)}
          >
            重跑
          </Button>
        )
      },
    },
  ]

  return (
    <div>
      <div
        style={{
          marginBottom: 16,
          display: 'flex',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <Space size={12} wrap>
          <Select
            style={{ minWidth: 220 }}
            placeholder="选择知识库"
            value={kbId || undefined}
            options={kbs.map((k) => ({ value: k.id, label: k.name }))}
            onChange={(v) => {
              setKbId(v)
              setStatusFilter('all')
            }}
            showSearch
            optionFilterProp="label"
          />
          <Select
            style={{ width: 130 }}
            value={statusFilter}
            options={[
              { value: 'all', label: '全部状态' },
              { value: 'pending', label: '待审查' },
              { value: 'running', label: '审查中' },
              { value: 'done', label: '已审查' },
              { value: 'failed', label: '审查失败' },
            ]}
            onChange={(v) => setStatusFilter(v)}
          />
        </Space>
        <Button
          icon={<ReloadOutlined />}
          onClick={() => void loadDocs(kbId)}
          loading={loading}
          disabled={!kbId}
        >
          刷新
        </Button>
      </div>

      <Table<Document>
        rowKey="id"
        columns={columns}
        dataSource={filtered}
        loading={loading}
        size="middle"
        pagination={{
          pageSize: 20,
          showSizeChanger: true,
          pageSizeOptions: ['20', '50', '100'],
          showTotal: (t) => `共 ${t} 条`,
        }}
        expandable={{
          // 展开行：四维依据 + 元信息（governance_report.dims.*.basis）
          expandedRowRender: (record) => <ReportDetail report={record.governance_report} record={record} />,
          rowExpandable: (record) => record.governance_status === 'done',
        }}
      />
    </div>
  )
}

// 展开行：四维审查依据与元信息
function ReportDetail({ report, record }: { report: GovernanceReport; record: Document }) {
  const dims = report?.dims ?? {}
  return (
    <div style={{ padding: '4px 8px' }}>
      <Descriptions size="small" column={2} bordered style={{ marginBottom: 12 }}>
        {Object.entries(DIM_LABEL).map(([key, label]) => (
          <Descriptions.Item key={key} label={`${label}依据`}>
            {dims[key]?.basis || '—'}
          </Descriptions.Item>
        ))}
      </Descriptions>
      <Descriptions size="small" column={3}>
        <Descriptions.Item label="治理权重">
          {typeof report?.governance_weight === 'number' ? report.governance_weight : '—'}
        </Descriptions.Item>
        <Descriptions.Item label="生效时间">
          {record.effective_at ? dayjs(record.effective_at).format('YYYY-MM-DD') : '—'}
        </Descriptions.Item>
        <Descriptions.Item label="失效时间">
          {record.expire_at ? dayjs(record.expire_at).format('YYYY-MM-DD') : '—'}
        </Descriptions.Item>
        <Descriptions.Item label="审查内容长度">
          {typeof report?.content_chars === 'number' ? `${report.content_chars} 字符` : '—'}
        </Descriptions.Item>
        <Descriptions.Item label="审查完成时间">
          {report?.reviewed_at ? dayjs(report.reviewed_at).format('YYYY-MM-DD HH:mm:ss') : '—'}
        </Descriptions.Item>
      </Descriptions>
    </div>
  )
}
