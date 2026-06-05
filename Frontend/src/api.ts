export type Threat = {
  category: string
  score: number
  explanation: string
}

export type EvaluateResponse = {
  verdict: 'allow' | 'allow_with_redaction' | 'block'
  overall_safety_score: number
  threats: Threat[]
  safe_response: string | null
  action_message: string
}

export async function evaluateGuardrails(input: {
  prompt: string
  model_response?: string
}): Promise<EvaluateResponse> {
  const apiUrl = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

  const res = await fetch(`${apiUrl}/evaluate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt: input.prompt,
      model_response: input.model_response ?? null,
    }),
  })

  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`API error: ${res.status} ${res.statusText}. ${text}`.trim())
  }

  return (await res.json()) as EvaluateResponse
}

