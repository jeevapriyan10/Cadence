/**
 * TypeScript interfaces mirroring the Cadence FastAPI backend Pydantic schemas.
 */

export type ProfileName = 'metro' | 'local' | 'mainline'

export interface GenerateNetworkRequest {
  profile_name: ProfileName | string
  seed?: number
  section_count?: number
  train_count?: number
  task_count?: number
}

export interface GenerateNetworkResponse {
  run_id: string
  profile_name: string
  seed: number
  section_count: number
  train_count: number
  task_count: number
}

export interface TrackSection {
  id: string
  name: string
  length_meters?: number | null
  attributes?: Record<string, unknown>
}

export interface SectionAdjacency {
  id: string
  section_a_id: string
  section_b_id: string
}

export interface TrainSlot {
  id: string
  name: string
  scheduled_start: string
  scheduled_end: string
  route: string[]
  priority: number
  attributes?: Record<string, unknown>
}

export interface MaintenanceTask {
  id: string
  name: string
  section_id: string
  duration_minutes: number
  earliest_start: string
  latest_end: string
  priority: number
  is_emergency: boolean
  attributes?: Record<string, unknown>
}

export interface ScheduledBlock {
  id: string
  task_id: string
  section_id: string
  start_time: string
  end_time: string
  method: string
  created_at: string
}

export interface NetworkDetailsResponse {
  run_id: string
  profile_name: string
  seed: number
  sections: TrackSection[]
  adjacencies: SectionAdjacency[]
  train_slots: TrainSlot[]
  maintenance_tasks: MaintenanceTask[]
}

export interface SolveRequest {
  run_id: string
  time_horizon_minutes?: number
  time_limit_seconds?: number
}

export interface SolveResponse {
  run_id: string
  status: string
  scheduled_blocks: ScheduledBlock[]
  objective_value?: number | null
  wall_time_seconds: number
}

export interface TrainImpact {
  train_slot_id: string
  train_name: string
  directly_affected: boolean
  delay_minutes: number
  affected_sections: string[]
  cascade_source?: string | null
}

export interface RippleResponse {
  run_id: string
  total_trains_affected: number
  total_delay_minutes: number
  per_train_impacts: TrainImpact[]
  summary: string
}

export type ConflictingEntityType = 'task' | 'train_slot' | 'safety_adjacency'

export interface RejectedCandidate {
  candidate_start: string
  candidate_end: string
  rejection_reason: string
  conflicting_entity_id: string
  conflicting_entity_type: ConflictingEntityType
}

export interface Explanation {
  task_id: string
  task_name: string
  scheduled_start: string
  scheduled_end: string
  chosen_reason: string
  rejected_candidates: RejectedCandidate[]
}

export interface ExplainResponse {
  run_id: string
  task_id: string
  explanation: Explanation | Record<string, unknown>
  summary: string
}

export interface ExplainAllResponse {
  run_id: string
  explanations: ExplainResponse[]
}

export interface ErrorResponse {
  detail: string
}
