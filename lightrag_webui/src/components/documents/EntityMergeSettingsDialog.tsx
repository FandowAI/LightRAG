import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Settings2Icon } from 'lucide-react'
import { toast } from 'sonner'

import Button from '@/components/ui/Button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/Dialog'
import {
  getEntityMergeSettings,
  previewEntityMerge,
  saveEntityMergeSettings,
  type EntityMergePreview,
  type EntityMergeSettings,
  type EntityMergeSnapshot
} from '@/api/entityMerge'
import { errorMessage } from '@/lib/utils'

export default function EntityMergeSettingsDialog() {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [snapshot, setSnapshot] = useState<EntityMergeSnapshot | null>(null)
  const [settings, setSettings] = useState<EntityMergeSettings | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [name, setName] = useState('')
  const [entityType, setEntityType] = useState('')
  const [preview, setPreview] = useState<EntityMergePreview | null>(null)
  const identityChecks = snapshot?.identity_verification?.enabled === true

  useEffect(() => {
    if (!open) return
    let active = true
    getEntityMergeSettings().then(data => {
      if (active) {
        setSnapshot(data)
        setSettings(data.settings)
      }
    }).catch(error => {
      if (active) toast.error(errorMessage(error))
    }).finally(() => {
      if (active) setLoading(false)
    })
    return () => { active = false }
  }, [open])

  const changeOpen = (next: boolean) => {
    if (next) {
      setLoading(true)
      setSnapshot(null)
      setPreview(null)
    }
    setOpen(next)
  }

  const save = async () => {
    if (!snapshot || !settings) return
    setSaving(true)
    try {
      const data = await saveEntityMergeSettings(snapshot.revision, settings)
      setSnapshot(data)
      setSettings(data.settings)
      toast.success(t('entityMerge.saved', '图谱写入设置已保存'))
    } catch (error) {
      toast.error(errorMessage(error))
    } finally {
      setSaving(false)
    }
  }

  const runPreview = async () => {
    setPreview(null)
    try {
      setPreview(await previewEntityMerge(name.trim(), entityType.trim()))
    } catch (error) {
      toast.error(errorMessage(error))
    }
  }

  const numberField = (
    label: string,
    key: 'similarity_threshold' | 'margin_threshold' | 'top_k',
    min: number,
    max: number,
    step: number
  ) => (
    <label className="flex items-center justify-between gap-4 text-sm">
      <span>{label}</span>
      <input
        aria-label={label}
        className="w-24 rounded border bg-background px-2 py-1 text-right"
        type="number"
        min={min}
        max={max}
        step={step}
        value={settings?.[key] ?? ''}
        onChange={event => setSettings(current => current && ({ ...current, [key]: Number(event.target.value) }))}
      />
    </label>
  )

  const valid = settings && Number.isFinite(settings.similarity_threshold)
    && settings.similarity_threshold >= 0 && settings.similarity_threshold <= 1
    && Number.isFinite(settings.margin_threshold)
    && settings.margin_threshold >= 0 && settings.margin_threshold <= 1
    && Number.isInteger(settings.top_k) && settings.top_k >= 1 && settings.top_k <= 100

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm"><Settings2Icon /> {t('entityMerge.button', '图谱写入设置')}</Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t('entityMerge.title', '实体语义合并')}</DialogTitle>
          <DialogDescription>{identityChecks
            ? t('entityMerge.identityDescription', '先查已确认词表，再由模型核对身份和原文证据。不确定时分开保留。')
            : t('entityMerge.description', '只在图谱写入时比较实体名称向量。保存后对后续文档生效。')}</DialogDescription>
        </DialogHeader>
        {loading && <p className="text-sm">{t('entityMerge.loading', '正在读取设置…')}</p>}
        {snapshot && settings && <div className="space-y-4">
          <div className="rounded border p-3 text-sm space-y-1">
            <div>{t('entityMerge.workspace', '工作区')}：{snapshot.workspace}</div>
            <div>{t('entityMerge.model', '向量模型')}：{snapshot.embedding_model || '—'} · {snapshot.embedding_dim}D</div>
            <div>{t('entityMerge.rules', '固定规则')}：{identityChecks
              ? t('entityMerge.identityRules', '相似度仅用于寻找候选；归属冲突不合并；合并必须有已确认词表或原文证据')
              : t('entityMerge.fixedRules', '仅比较已入库实体；实体类型必须一致；只使用名称向量')}</div>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={settings.enabled}
              onChange={event => setSettings({ ...settings, enabled: event.target.checked })} />
            {identityChecks ? t('entityMerge.identityEnabled', '启用模型身份核对（已确认词表始终生效）') : t('entityMerge.enabled', '启用实体语义合并')}
          </label>
          <div className="space-y-3 rounded border p-3">
            {numberField(t('entityMerge.similarity', '名称余弦相似度下限'), 'similarity_threshold', 0, 1, 0.01)}
            {!identityChecks && numberField(t('entityMerge.margin', '第一、第二候选分差下限'), 'margin_threshold', 0, 1, 0.01)}
            {numberField(t('entityMerge.topK', '候选数量 Top K'), 'top_k', 1, 100, 1)}
          </div>
          <p className="text-muted-foreground text-xs">
            {identityChecks
              ? t('entityMerge.identityHint', '候选分数不是身份确认结果。设置仅对后续写入生效，不自动修改历史图谱。')
              : t('entityMerge.hint', '默认 0.90 / 0.05 / 20 是试运行值；首批文档没有已入库候选。修改不会追溯已写入图谱。')}
          </p>
          <div className="flex justify-end">
            <Button disabled={!valid || !snapshot.write_enabled || saving} onClick={save}>
              {saving ? t('entityMerge.saving', '保存中…') : t('entityMerge.save', '保存设置')}
            </Button>
          </div>
          <div className="space-y-2 border-t pt-4">
            <p className="font-medium text-sm">{t('entityMerge.preview', '按已保存配置试算候选（不会写库）')}</p>
            <div className="flex flex-wrap gap-2">
              <input className="min-w-36 flex-1 rounded border bg-background px-2 py-1 text-sm" value={name}
                placeholder={t('entityMerge.name', '实体名称')} onChange={event => setName(event.target.value)} />
              <input className="min-w-28 flex-1 rounded border bg-background px-2 py-1 text-sm" value={entityType}
                placeholder={t('entityMerge.type', '实体类型')} onChange={event => setEntityType(event.target.value)} />
              <Button variant="outline" disabled={!name.trim() || !entityType.trim()} onClick={runPreview}>
                {t('entityMerge.runPreview', '试算')}
              </Button>
            </div>
            {preview && <div className="max-h-48 overflow-y-auto rounded border p-2 text-sm">
              <p>{t('entityMerge.decision', '判定')}：{preview.decision === 'candidate'
                ? t('entityMerge.identityPreview', '仅展示名称候选；实际写入时自动核对身份和原文证据')
                : preview.decision === 'merge'
                  ? `${t('entityMerge.merge', '合并到')} ${preview.target}`
                  : preview.decision === 'exact_name'
                    ? t('entityMerge.exactName', '已有完全同名实体')
                    : t('entityMerge.create', '创建新实体')}</p>
              {preview.candidates.map(candidate => <div key={candidate.name} className="flex justify-between gap-2 border-t py-1">
                <span>{candidate.name} · {candidate.type}</span><span>{candidate.score.toFixed(4)}</span>
              </div>)}
            </div>}
          </div>
        </div>}
      </DialogContent>
    </Dialog>
  )
}
