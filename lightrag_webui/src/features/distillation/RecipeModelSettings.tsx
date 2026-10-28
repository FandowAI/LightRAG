import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import Button from '@/components/ui/Button'
import Input from '@/components/ui/Input'
import type { ModelFieldSchema, RecipeSettingsSnapshot, RecipeValues } from '@/api/dataWorkflow'

export default function RecipeModelSettings({
  snapshot,
  values,
  onChange
}: {
  snapshot: RecipeSettingsSnapshot
  values: RecipeValues
  onChange: (values: RecipeValues) => void
}) {
  const { t } = useTranslation()
  const [newName, setNewName] = useState('')
  const fields = snapshot.model_schema.properties ?? {}
  const set = (name: string, key: string, value: string | number | null) =>
    onChange({
      ...values,
      models: { ...values.models, [name]: { ...values.models[name], [key]: value } }
    })
  const label = (key: string) => t(`distillation.settings.${key}`)

  function control(name: string, key: string, raw: ModelFieldSchema) {
    const field = raw.anyOf?.find((item) => item.type !== 'null') ?? raw
    const optional = raw.anyOf?.some((item) => item.type === 'null')
    const value = values.models[name][key] ?? raw.default ?? ''
    const text = `${label(`modelFields.${key}`)} (${key})`
    return (
      <label key={key} className="space-y-1 text-sm">
        <span className="block">{text}</span>
        {field.enum ? (
          <select
            aria-label={text}
            className="bg-background w-full rounded-md border p-2"
            value={String(value)}
            onChange={(e) => set(name, key, e.target.value || null)}
          >
            {optional && <option value="">{label('unset')}</option>}
            {field.enum.map((option) => (
              <option key={option}>{option}</option>
            ))}
          </select>
        ) : (
          <Input
            type={field.type === 'integer' || field.type === 'number' ? 'number' : 'text'}
            value={value}
            required={snapshot.model_schema.required?.includes(key)}
            step={field.type === 'integer' ? 1 : 'any'}
            min={
              field.minimum ??
              (field.exclusiveMinimum === 0 ? (field.type === 'integer' ? 1 : 0.001) : undefined)
            }
            max={field.maximum}
            placeholder={optional ? label('unset') : ''}
            onChange={(e) =>
              set(
                name,
                key,
                e.target.value === '' && optional
                  ? null
                  : field.type === 'integer' || field.type === 'number'
                    ? Number(e.target.value)
                    : e.target.value
              )
            }
          />
        )}
      </label>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-muted-foreground text-sm">{label('modelHint')}</p>
      {Object.entries(values.models).map(([name]) => (
        <details key={name} open className="space-y-3 rounded-md border p-3">
          <summary className="cursor-pointer font-medium">{name}</summary>
          <div className="grid gap-3 sm:grid-cols-2">
            {Object.entries(fields).map(([key, field]) => control(name, key, field))}
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={Object.values(values.stages).some((stage) => stage.model === name)}
            onClick={() => {
              const models = { ...values.models }
              delete models[name]
              onChange({ ...values, models })
            }}
          >
            {label('removeModel')}
          </Button>
        </details>
      ))}
      <div className="flex gap-2">
        <Input
          aria-label={label('profileName')}
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
        />
        <Button
          type="button"
          variant="outline"
          disabled={!/^\S+$/.test(newName) || newName in values.models}
          onClick={() => {
            onChange({
              ...values,
              models: {
                ...values.models,
                [newName]: { api_url: '', model_name: '', key_name_of_api_key: 'DF_API_KEY' }
              }
            })
            setNewName('')
          }}
        >
          {label('addModel')}
        </Button>
      </div>
    </div>
  )
}
