import { useState } from 'react'
import { App, Button, Card, Col, Empty, Row, Statistic, Table, Tag, Typography } from 'antd'
import { PlayCircleOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'

import { EvalCaseResult, runEval } from '../../mocks/data'
import { errMsg } from './common'

const { Text } = Typography

export function EvalPanel() {
  const { message } = App.useApp()
  const [loading, setLoading] = useState(false)
  const [cases, setCases] = useState<EvalCaseResult[]>([])
  const [summary, setSummary] = useState<{
    total: number
    passed: number
    failed: number
    answer_accuracy: number
    refuse_accuracy: number
    total_seconds: number
  } | null>(null)

  const handleRun = async () => {
    setLoading(true)
    try {
      const report = await runEval()
      setCases(report.cases)
      setSummary({
        total: report.total,
        passed: report.passed,
        failed: report.failed,
        answer_accuracy: report.answer_accuracy,
        refuse_accuracy: report.refuse_accuracy,
        total_seconds: report.total_seconds,
      })
      message.success(`评估完成：${report.passed}/${report.total} 通过`)
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }

  const columns: ColumnsType<EvalCaseResult> = [
    { title: '问题', dataIndex: 'question', key: 'question', ellipsis: true },
    {
      title: '期望',
      key: 'expected',
      width: 80,
      render: (_, r) => (
        <Tag color={r.expected_answerable ? 'blue' : 'default'}>
          {r.expected_answerable ? '可答' : '拒答'}
        </Tag>
      ),
    },
    {
      title: '实际',
      key: 'actual',
      width: 80,
      render: (_, r) => (
        <Tag color={r.actual_refused ? 'orange' : 'green'}>
          {r.actual_refused ? '拒答' : '回答'}
        </Tag>
      ),
    },
    {
      title: '结果',
      key: 'passed',
      width: 80,
      render: (_, r) => (
        <Tag color={r.passed ? 'success' : 'error'}>
          {r.passed ? 'PASS' : 'FAIL'}
        </Tag>
      ),
    },
    {
      title: '命中chunk',
      dataIndex: 'chunks_count',
      key: 'chunks_count',
      width: 90,
    },
    {
      title: '耗时',
      key: 'elapsed_ms',
      width: 80,
      render: (_, r) => `${r.elapsed_ms}ms`,
    },
    {
      title: '拒答原因',
      dataIndex: 'refuse_reason',
      key: 'refuse_reason',
      width: 160,
      render: (v: string | null) => v || '—',
    },
  ]

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 12 }}>
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={handleRun}
          loading={loading}
        >
          运行评估
        </Button>
        <Text type="secondary">
          对 eval_cases 表中的全部用例跑 retrieve + generate，检查可答/拒答是否符合预期
        </Text>
      </div>

      {summary && (
        <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
          <Col xs={24} sm={6}>
            <Card variant="borderless">
              <Statistic title="总数" value={summary.total} />
            </Card>
          </Col>
          <Col xs={24} sm={6}>
            <Card variant="borderless">
              <Statistic
                title="通过"
                value={summary.passed}
                valueStyle={{ color: '#52c41a' }}
                suffix={`/ ${summary.failed} 失败`}
              />
            </Card>
          </Col>
          <Col xs={24} sm={6}>
            <Card variant="borderless">
              <Statistic
                title="可答准确率"
                value={summary.answer_accuracy}
                suffix="%"
              />
            </Card>
          </Col>
          <Col xs={24} sm={6}>
            <Card variant="borderless">
              <Statistic
                title="拒答准确率"
                value={summary.refuse_accuracy}
                suffix="%"
              />
            </Card>
          </Col>
        </Row>
      )}

      {cases.length > 0 ? (
        <Card variant="borderless" styles={{ body: { padding: 16 } }}>
          <Table<EvalCaseResult>
            rowKey={(_, idx) => String(idx)}
            columns={columns}
            dataSource={cases}
            size="small"
            pagination={{ pageSize: 20 }}
          />
        </Card>
      ) : !loading ? (
        <Card variant="borderless">
          <Empty description="点击「运行评估」开始" />
        </Card>
      ) : null}
    </div>
  )
}
