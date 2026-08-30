export type JsonObject = Record<string, unknown>;

export type WorkflowId = string & { readonly __brand: 'WorkflowId' };
export type ArtifactId = string & { readonly __brand: 'ArtifactId' };
export type EventId = string & { readonly __brand: 'EventId' };

export type ResultStatus =
  | 'IN_PROGRESS' | 'SUCCEEDED' | 'PARTIAL_SUCCESS' | 'FAILED' | 'CANCELLED';
export type WorkflowStatus = 'DRAFT' | 'ACTIVE' | 'ABANDONED' | 'CLOSED';
export type ExecutionState =
  | 'CREATED' | 'PREPARING' | 'RUNNING' | 'WAITING_RETRY'
  | 'WAITING_USER' | 'RECOVERING' | 'BLOCKED' | 'TERMINAL';
export type ControlState =
  | 'RUNNING' | 'PAUSE_REQUESTED' | 'PAUSED' | 'TERMINATING' | 'TERMINATED';
export type CleanupState =
  | 'NONE' | 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'DEFERRED';

export type CommandTarget =
  | { target_type: 'STEP'; step_id: string }
  | { target_type: 'ITEM'; step_id: string; item_id: string }
  | { target_type: 'WORK_UNIT'; work_unit_id: string }
  | { target_type: 'WORK_UNIT_ATTEMPT'; work_unit_attempt_id: string }
  | { target_type: 'PROVIDER_RECEIPT'; provider_receipt_id: string }
  | { target_type: 'EXTERNAL_OPERATION'; external_operation_id: string };

export interface WorkflowSnapshot {
  workflow_id: WorkflowId;
  workflow_group_id: string;
  group_state_version: number;
  parent_workflow_id: string | null;
  result_status: ResultStatus;
  execution_state: ExecutionState;
  control_state: ControlState;
  cleanup_state: CleanupState;
  status: WorkflowStatus;
  state_version: number;
  draft_revision: number;
  current_step_id: string | null;
  source_artifact_id: ArtifactId | null;
  item_count: number;
  artifact_count: number;
  latest_event_id: EventId | null;
  latest_seq: number;
  last_error_code: string | null;
  last_error_message: string | null;
  latest_event?: WorkflowEvent;
  updated_at: string;
}

export interface ArtifactInfo {
  artifact_id: ArtifactId;
  workflow_id: WorkflowId;
  item_id?: string | null;
  step_id?: string | null;
  work_unit_id?: string | null;
  artifact_type: string;
  lifecycle_state: 'TEMP' | 'READY' | 'INVALID' | 'DELETED';
  sha256: string | null;
  size_bytes: number | null;
  format: string | null;
  verified: boolean;
  created_at: string;
  updated_at: string;
}

export type RetryScope = 'NONE' | 'WORKFLOW' | 'ITEMS';
export type WorkspaceActionKind = 'SERVICE' | 'UI';
export type WorkspaceActionType =
  | 'PARSE' | 'SAVE_CONFIGURATION' | 'GENERATE' | 'PAUSE' | 'RESUME'
  | 'CANCEL' | 'RETRY' | 'RECONCILE' | 'RESOLVE' | 'ARCHIVE'
  | 'ABANDON' | 'RERUN' | 'EXPORT_ZIP' | 'OPEN_VIEW' | 'DOWNLOAD_ARTIFACT'
  | 'DOWNLOAD_ZIP' | 'RECONNECT';

export interface WorkspaceAction {
  kind: WorkspaceActionKind;
  type: WorkspaceActionType;
  enabled: boolean;
  reason: string | null;
  target: JsonObject | null;
  expected_state_version: number | null;
  expected_target_state_version: number | null;
  expected_group_state_version: number | null;
  safe_to_retry: boolean;
  retry_scope: RetryScope;
}

export interface WorkspaceBlocker {
  code: string;
  title: string;
  message: string;
  severity: 'INFO' | 'WARNING' | 'ERROR' | 'BLOCKING';
  affected_item_ids: string[];
  retryable: boolean;
  safe_to_retry: boolean;
  retry_scope: RetryScope;
  requires_reconcile: boolean;
  recovery_action: WorkspaceAction | null;
}

export interface WorkspaceItem {
  item_id: string;
  item_identity_key: string;
  sequence: number;
  item_type: string;
  normalized_content: string | null;
  content_ref: {
    content_id: string;
    size_bytes: number;
    content_hash: string;
    max_response_bytes: number;
  } | null;
  source_locator: string | null;
  metadata: JsonObject;
  skip_reason: string | null;
  content_hash: string;
  status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'AMBIGUOUS' | 'CANCELLED' | 'SKIPPED' | 'UNRESOLVED';
  role: string | null;
  voice_key: string | null;
  attempt_count: number;
  error_code: string | null;
  user_message: string | null;
  retry_scope: RetryScope;
  requires_reconcile: boolean;
  artifact_ids: string[];
  updated_at: string;
}

export interface WorkspaceProvider {
  provider: string;
  status: 'UNKNOWN' | 'READY' | 'LOGIN_REQUIRED' | 'EXPIRED' | 'UNAVAILABLE' | 'DISABLED';
  ready: boolean;
  reason: string;
  can_generate: boolean;
  /** True when a foreground generate command may open/login the provider. */
  can_start_generation: boolean;
}

export interface ItemContentResponse {
  workflow_id: WorkflowId;
  item_id: string;
  content_id: string;
  state_version: number;
  item_state_version: number;
  content_hash: string;
  size_bytes: number;
  offset_bytes: number;
  next_offset_bytes: number;
  truncated: boolean;
  content: string;
}

export interface WorkspaceArtifact {
  artifact_id: string;
  workflow_id: WorkflowId;
  item_id: string | null;
  step_id: string | null;
  work_unit_id: string | null;
  artifact_type: string;
  lifecycle_state: 'TEMP' | 'READY' | 'INVALID' | 'DELETED';
  format: string | null;
  extension: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  sha256: string | null;
  verified: boolean;
  filename: string | null;
  duration_ms: number | null;
  producer: string;
  producer_version: string;
  created_at: string;
  updated_at: string;
}

export interface EffectiveConfiguration {
  provider: string;
  generation_mode: 'composite_cut' | 'single_segment';
  format: string;
  quality: string | null;
  preview: boolean;
  preview_limit: number | null;
  rate: number | null;
  pitch: number | null;
  volume: number | null;
  default_female_voice: string | null;
  default_male_voice: string | null;
  role_voices: Record<string, string | null>;
  role_configs: Record<string, { rate: number | null; pitch: number | null; volume: number | null }>;
}

export interface ConfigurationProjection {
  configuration_revision: number;
  configuration_hash: string;
  effective: EffectiveConfiguration;
  source_priority: Record<string, 'GLOBAL' | 'ROLE' | 'ITEM'>;
  frozen_fields: string[];
}

export interface WorkflowWorkspace {
  schema_version: number;
  source_filename: string;
  snapshot: WorkflowSnapshot;
  progress: {
    total: number;
    completed: number;
    failed: number;
    cancelled: number;
    skipped: number;
    pending: number;
    deliverable: number;
    percent: number;
    deliverable_percent: number;
  };
  blockers: WorkspaceBlocker[];
  available_actions: WorkspaceAction[];
  current_target: { item_id: string; label: string; started_at: string } | null;
  items: WorkspaceItem[];
  artifacts: WorkspaceArtifact[];
  configuration: ConfigurationProjection;
  provider: WorkspaceProvider;
  delivery: {
    zip_artifact_id: string | null;
    zip_available: boolean;
    included_item_ids: string[];
    excluded_item_ids: string[];
    exclusion_reasons: Record<string, string>;
  };
  sync: { state_version: number; last_event_id: string | null; requires_resync: boolean };
}

export interface ActiveWorkflowCandidate {
  workflow: WorkflowSnapshot;
  can_resume: boolean;
  can_takeover: boolean;
  resume_reason: string;
  requires_reconcile: boolean;
}

export interface ActiveWorkflowPage {
  workflows: ActiveWorkflowCandidate[];
  limit: number;
  truncated: boolean;
}

export interface WorkflowCommandRequest {
  expected_state_version: number;
  reason?: string;
}

export interface TargetedCommandRequest {
  expected_state_version: number;
  expected_target_state_version: number;
  expected_attempt_id?: string;
  reason?: string;
  target: CommandTarget;
}

export interface CommandResponse {
  request_id: string;
  workflow_id: WorkflowId;
  accepted_action: string;
  result_status: ResultStatus;
  execution_state: ExecutionState;
  control_state: ControlState;
  cleanup_state: CleanupState;
  state_version: number;
  current_snapshot: WorkflowSnapshot;
  target_attempt_id: string | null;
}

export interface WorkflowDeleteResponse {
  request_id: string;
  workflow_id: WorkflowId;
  accepted_action: 'delete';
  deleted: true;
}

export interface WorkflowEvent {
  event_id: EventId;
  seq: number;
  workflow_id: WorkflowId;
  mutation_id: string;
  schema_version: string;
  step_id: string | null;
  item_id: string | null;
  attempt_id: string | null;
  correlation_id: string;
  causation_id: string | null;
  actor_type: 'USER' | 'WORKER' | 'RECOVERY' | 'SCHEDULER' | 'SYSTEM';
  actor_id: string | null;
  event_type: string;
  phase: string | null;
  payload: JsonObject;
  created_at: string;
}

export interface SnapshotEnvelope {
  kind: 'snapshot';
  workflow_id: WorkflowId;
  snapshot_seq: number;
  snapshot_event_id: EventId | null;
  state: WorkflowSnapshot;
}

export type SseFrame =
  | { kind: 'event'; event: WorkflowEvent }
  | { kind: 'snapshot'; snapshot: SnapshotEnvelope };

export interface WorkflowEventStream {
  onFrame(listener: (frame: SseFrame) => void): () => void;
  onError(listener: (error: Error) => void): () => void;
  close(): Promise<void>;
}

export interface SourceImportStatus {
  source_import_id: string;
  workflow_id: WorkflowId;
  staging_generation: number;
  status: 'CREATED' | 'RECEIVING' | 'READY' | 'FAILED' | 'EXPIRED' | 'ABORTED';
  state_version: number;
  received_size_bytes: number;
  actual_size_bytes: number | null;
  actual_sha256: string | null;
  source_artifact_id: ArtifactId | null;
  error_code: string | null;
  expires_at: string;
  updated_at: string;
}

export interface SourceImportGenerationStatus extends Omit<SourceImportStatus, 'staging_generation'> {
  generation: number;
}

// Renderer can see only this narrow, typed surface.  Capability tokens,
// staging paths, storage keys and generic fetch are intentionally absent.
export interface DesktopWorkflowApi {
  getWorkflow(workflowId: WorkflowId): Promise<WorkflowSnapshot>;
  getWorkspace(workflowId: WorkflowId): Promise<WorkflowWorkspace>;
  getItemContent(workflowId: WorkflowId, itemId: string, contentId: string, expectedStateVersion?: number): Promise<ItemContentResponse>;
  getConfig(): Promise<JsonObject>;
  listWorkflows(limit?: number): Promise<WorkflowHistoryRecord[]>;
  listActiveWorkflows(limit?: number): Promise<ActiveWorkflowCandidate[]>;
  listActiveWorkflowPage(limit?: number): Promise<ActiveWorkflowPage>;
  listItems(workflowId: WorkflowId): Promise<Array<JsonObject>>;
  listArtifacts(workflowId: WorkflowId, limit?: number): Promise<ArtifactInfo[]>;
  createWorkflow(input: {
    workflow_type: string;
    business_key?: string;
    configuration: JsonObject;
  }): Promise<WorkflowSnapshot>;
  patchDraft(workflowId: WorkflowId, input: {
    expected_state_version: number;
    configuration_revision?: number;
    configuration?: JsonObject;
    item_overrides?: Array<{ item_id: string; patch: JsonObject }>;
  }): Promise<WorkflowSnapshot>;
  patchWorkspace(workflowId: WorkflowId, input: {
    expected_state_version: number;
    configuration_revision?: number;
    configuration?: JsonObject;
    item_overrides?: Array<{ item_id: string; patch: JsonObject }>;
  }): Promise<WorkflowWorkspace>;
  rerun(workflowId: WorkflowId, input: {
    expected_group_state_version: number;
    source_workflow_id?: string;
    reason?: string;
  }): Promise<WorkflowSnapshot>;
  sendCommand(workflowId: WorkflowId, action: 'parse' | 'generate' | 'pause' | 'resume' | 'cancel' | 'export-zip',
              input: WorkflowCommandRequest): Promise<CommandResponse>;
  parseWorkflow(workflowId: WorkflowId, input: ParseRequest): Promise<CommandResponse & {
    parse_results?: Array<JsonObject>;
    source_filename?: string;
    source_artifact_id?: ArtifactId;
    parsed_artifact_id?: ArtifactId;
  }>;
  generateWorkflow(workflowId: WorkflowId, input: GenerateRequest): Promise<CommandResponse>;
  createExportZip(workflowId: WorkflowId, input: {
    expected_state_version: number;
    include_item_ids?: string[];
  }): Promise<JsonObject | null>;
  archiveWorkflow(workflowId: WorkflowId, input: WorkflowCommandRequest): Promise<CommandResponse>;
  deleteWorkflow(workflowId: WorkflowId, input: WorkflowCommandRequest): Promise<WorkflowDeleteResponse>;
  retry(workflowId: WorkflowId, input: TargetedCommandRequest): Promise<CommandResponse>;
  reconcile(workflowId: WorkflowId, input: TargetedCommandRequest): Promise<CommandResponse>;
  resolve(input: {
    attempt_id: string;
    target: CommandTarget;
    expected_state_version: number;
    expected_target_state_version: number;
    decision: 'CONFIRMED' | 'NOT_SUBMITTED' | 'BLOCKED';
    evidence: { source: string; evidence_hash: string; reference?: string; summary?: string };
  }): Promise<CommandResponse>;
  openWorkflowEvents(workflowId: WorkflowId, lastEventId: EventId | null): Promise<WorkflowEventStream>;
  createSourceImport(workflowId: WorkflowId, input: {
    metadata: JsonObject;
    expected_size_bytes?: number | null;
    expected_sha256?: string | null;
    content_type?: string | null;
  }): Promise<SourceImportStatus>;
  // Preload acquires the opaque write grant internally.  Renderer never sends
  // a raw writer fencing token or a staging path.
  writeSourceImport(importId: string, generation: number, content: ReadableStream<Uint8Array>): Promise<SourceImportStatus>;
  getSourceImport(importId: string): Promise<SourceImportStatus>;
  getSourceImportGeneration(importId: string, generation: number): Promise<SourceImportGenerationStatus>;
  openArtifact(artifactId: ArtifactId): Promise<ReadableStream<Uint8Array>>;
}

export interface ParseRequest {
  expected_state_version: number;
  source_artifact_id?: ArtifactId;
}

export interface GenerateRequest extends WorkflowCommandRequest {
  configuration_revision?: number;
  generation_mode?: 'composite_cut' | 'single_segment';
  provider?: string;
  account_scope?: string;
  item_ids?: string[];
}

export interface WorkflowHistoryRecord {
  id: WorkflowId;
  workflow_id: WorkflowId;
  source_filename: string;
  available_files: number;
  completed: number;
  failed: number;
  cancelled: number;
  skipped: number;
  total: number;
  pending: number;
  format: string;
  generation_mode: string;
  preview: boolean;
  zip_available: boolean;
  zip_artifact_id: ArtifactId | null;
  failed_items: Array<JsonObject>;
  status: WorkflowStatus;
  result_status: ResultStatus;
  execution_state: ExecutionState;
  control_state: ControlState;
  state_version: number;
  can_delete: boolean;
  delete_reason: string | null;
  created_at: string;
  completed_at: string;
  updated_at: string;
}

declare global {
  interface Window {
    electronAPI: { workflow: DesktopWorkflowApi };
  }
}
