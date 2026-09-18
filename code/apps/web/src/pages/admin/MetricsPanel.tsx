import { useEffect, useState } from 'react'
import { App, Button, Card, Col, Empty, Row, Select, Skeleton, Spin, Statistic, Typography } from 'antd'

import { fetchMetrics, MetricSummary } from '../../mocks/data'
import { errMsg } from './common'

const { Text } = Typography

export function MetricsPanel() {
  const { message } = App.useApp()
  const [data, setData] = useState<MetricSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [hours, setHours] = useState(24)

  const load = () => {
    setLoading(true)
    fetchMetrics(hours)
      .then(setData)
      .catch((e) => message.error(errMsg(e)))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [hours])

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 12 }}>
        <Select
          value={hours}
          onChange={setHours}
          style={{ width: 160 }}
          options={[
            { value: 1, label: '最近 1 小时' },
            { value: 6, label: '最近 6 小时' },
            { value: 24, label: '最近 24 小时' },
            { value: 72, label: '最近 3 天' },
            { value: 168, label: '最近 7 天' },
          ]}
        />
        <Button onClick={load} loading={loading}>刷新</Button>
        {data && <Text type="secondary">{data.window}</Text>}
      </div>

      {loading ? (
        <Skeleton active />
      ) : !data || data.total_requests === 0 ? (
        <Card variant="borderless">
          <Empty description="所选时间范围内暂无问答数据" />
        </Card>
      ) : (
        <>
          {/* 概览统计 */}
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            <Col xs={24} sm={8}>
              <Card variant="borderless">
                <Statistic title="总请求数" value={data.total_requests} />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card variant="borderless">
                <Statistic
                  title="拒答数"
                  value={data.refused_count}
                  suffix={`/ ${data.refused_rate}%`}
                />
              </Card>
            </Col>
            <Col xs={24} sm={8}>
              <Card variant="borderless">
                <Statistic title="平均端到端延迟" value={data.total_avg} suffix="ms" />
              </Card>
            </Col>
          </Row>

          {/* 端到端延迟分位数 */}
          <Card title="端到端延迟（total_ms）" variant="borderless" style={{ marginBottom: 16 }}>
            <Row gutter={[16, 16]}>
              <Col xs={24} sm={6}>
                <Statistic title="P50（中位数）" value={data.total_p50} suffix="ms" />
              </Col>
              <Col xs={24} sm={6}>
                <Statistic title="P95" value={data.total_p95} suffix="ms" />
              </Col>
              <Col xs={24} sm={6}>
                <Statistic title="P99" value={data.total_p99} suffix="ms" />
              </Col>
              <Col xs={24} sm={6}>
                <Statistic title="平均" value={data.total_avg} suffix="ms" />
              </Col>
            </Row>
          </Card>

          {/* 检索延迟分位数 */}
          <Card title="检索延迟（retrieve_ms）" variant="borderless">
            <Row gutter={[16, 16]}>
              <Col xs={24} sm={6}>
                <Statistic title="P50" value={data.retrieve_p50} suffix="ms" />
              </Col>
              <Col xs={24} sm={6}>
                <Statistic title="P95" value={data.retrieve_p95} suffix="ms" />
              </Col>
              <Col xs={24} sm={6}>
                <Statistic title="P99" value={data.retrieve_p99} suffix="ms" />
              </Col>
              <Col xs={24} sm={6}>
                <Statistic title="平均" value={data.retrieve_avg} suffix="ms" />
              </Col>
            </Row>
          </Card>
        </>
      )}
    </div>
  )
}
