import type {
  ExplainAllResponse,
  ExplainResponse,
  GenerateNetworkRequest,
  GenerateNetworkResponse,
  NetworkDetailsResponse,
  RippleResponse,
  SolveRequest,
  SolveResponse,
} from './types'

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL?.replace(/\/+$/, '') || 'http://localhost:8000'

class ApiError extends Error {
  status: number
  detail?: string

  constructor(message: string, status: number, detail?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${API_BASE_URL}${path}`
  const headers = {
    'Content-Type': 'application/json',
    ...(options?.headers || {}),
  }

  let response: Response
  try {
    response = await fetch(url, {
      ...options,
      headers,
    })
  } catch (err) {
    const errorMsg = err instanceof Error ? err.message : String(err)
    throw new ApiError(
      `Network request failed to ${url}: ${errorMsg}. Please ensure the Cadence backend is running at ${API_BASE_URL}.`,
      0,
    )
  }

  if (!response.ok) {
    let errorDetail = response.statusText
    try {
      const errorJson = await response.json()
      if (errorJson && typeof errorJson.detail === 'string') {
        errorDetail = errorJson.detail
      } else if (errorJson && typeof errorJson.detail === 'object') {
        errorDetail = JSON.stringify(errorJson.detail)
      }
    } catch {
      // Body was not JSON; use statusText
    }
    throw new ApiError(
      `API Error [${response.status}]: ${errorDetail}`,
      response.status,
      errorDetail,
    )
  }

  return response.json() as Promise<T>
}

/**
 * Generate a reproducible synthetic railway network shaped by a NetworkProfile.
 * POST /networks/generate
 */
export async function generateNetwork(
  data: GenerateNetworkRequest,
): Promise<GenerateNetworkResponse> {
  return request<GenerateNetworkResponse>('/networks/generate', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

/**
 * Retrieve full details of a generated railway network by run_id.
 * GET /networks/{run_id}
 */
export async function getNetwork(runId: string): Promise<NetworkDetailsResponse> {
  return request<NetworkDetailsResponse>(`/networks/${encodeURIComponent(runId)}`, {
    method: 'GET',
  })
}

/**
 * Solve the maintenance possession block scheduling problem using OR-Tools CP-SAT.
 * POST /solve
 */
export async function solveSchedule(data: SolveRequest): Promise<SolveResponse> {
  return request<SolveResponse>('/solve', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

/**
 * Simulate downstream cascade delay propagation for a solved schedule using Ripple.
 * GET /ripple/{run_id}
 */
export async function getRipple(runId: string): Promise<RippleResponse> {
  return request<RippleResponse>(`/ripple/${encodeURIComponent(runId)}`, {
    method: 'GET',
  })
}

/**
 * Reconstruct post-hoc why a specific maintenance task was scheduled in its slot.
 * GET /reason/{run_id}/{task_id}
 */
export async function getExplanation(
  runId: string,
  taskId: string,
): Promise<ExplainResponse> {
  return request<ExplainResponse>(
    `/reason/${encodeURIComponent(runId)}/${encodeURIComponent(taskId)}`,
    {
      method: 'GET',
    },
  )
}

/**
 * Generate post-hoc explanations for every scheduled maintenance possession task in a run.
 * GET /reason/{run_id}
 */
export async function getAllExplanations(runId: string): Promise<ExplainAllResponse> {
  return request<ExplainAllResponse>(`/reason/${encodeURIComponent(runId)}`, {
    method: 'GET',
  })
}

export { ApiError }
