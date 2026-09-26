import { createAsyncThunk, createSlice, type PayloadAction } from '@reduxjs/toolkit';
import { getOp, postOp } from '../../api/client';
import { agentGrantWirePayload, type GrantAgentAccessArgs } from './agentGrantPayload';
import {
  controlCardGetRequest,
  type ProjectControlCoordinates,
} from './projectPersonControl';
import {
  projectAgentCardGetRequest,
  projectAgentCardRecord,
  type ProjectAgentCardTarget,
} from './projectAgentCard';
import type {
  DelegatedAccessCreateResult,
  DelegatedAccessGrantOption,
  DelegatedAccessListResult,
  DelegatedAccessRecord,
  DelegatedAccessRenewResult,
  DelegatedAccessResourceOperations,
  DelegatedAccessResourceOption,
  DelegatedAccessRevokeResult,
  DelegatedAccessStoredNamedServices,
  DelegatedInvocationPolicyResult,
  ControlCardGetResult,
  ProjectAgentCardGetResult,
  ProjectPersonControlViewer,
} from '../../api/types';

export interface DelegatedAccessState {
  platformUserId: string;
  items: DelegatedAccessRecord[];
  /** One credentialless Card opened by an exact deep link. It is deliberately
   *  separate from `items`, because that normal caller-card list does not
   *  centrally enumerate Control Cards yet. */
  focusedCard?: DelegatedAccessRecord;
  /** W260: the viewer's rights on a focused project-held person Control Card. */
  focusedViewer?: ProjectPersonControlViewer;
  grantOptions: DelegatedAccessGrantOption[];
  resources: DelegatedAccessResourceOption[];
  issuedToken: string;
  issuedHeader: string;
  issuedAccess?: DelegatedAccessRecord;
  loading: boolean;
  loadRequestId: string;
  busy: boolean;
  error: string;
}

const initialState: DelegatedAccessState = {
  platformUserId: '',
  items: [],
  focusedCard: undefined,
  grantOptions: [],
  resources: [],
  issuedToken: '',
  issuedHeader: '',
  loading: true,
  loadRequestId: '',
  busy: false,
  error: '',
};

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function resultError(result: { error?: string; message?: string } | null | undefined, fallback: string): string {
  return result?.message || result?.error || fallback;
}

export const loadDelegatedAccess = createAsyncThunk<DelegatedAccessListResult, void, { rejectValue: string }>(
  'delegatedAccess/load',
  async (_arg, { rejectWithValue }) => {
    try {
      const res = await getOp<DelegatedAccessListResult>('delegated_access_list');
      if (res?.ok === false) return rejectWithValue(resultError(res, 'Failed to load delegated access'));
      return res || {};
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

export const loadControlCard = createAsyncThunk<
  ControlCardGetResult,
  { controlId: string; projectRef?: string; targetSubject?: string; invitationRef?: string },
  { rejectValue: string }
>(
  'delegatedAccess/loadControlCard',
  async ({ controlId, projectRef, targetSubject, invitationRef }, { rejectWithValue }) => {
    try {
      const request = controlCardGetRequest({
        controlId,
        projectRef,
        targetSubject,
        invitationRef,
      });
      const res = await postOp<ControlCardGetResult>(
        request.operation,
        request.data,
      );
      if (res?.ok === false) return rejectWithValue(resultError(res, 'Failed to load the Control Card'));
      if (!res?.access) return rejectWithValue('Connection Hub returned no editable Card');
      if (res.access.state && res.access.state !== 'active') {
        return rejectWithValue('This Control Card is revoked. Linked callers remain closed until the application links an active Card.');
      }
      return res;
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

/** W319: another person's agent Card, opened through a project the agent attends. */
export const loadProjectAgentCard = createAsyncThunk<
  DelegatedAccessRecord,
  ProjectAgentCardTarget,
  { rejectValue: string }
>(
  'delegatedAccess/loadProjectAgentCard',
  async (target, { rejectWithValue }) => {
    try {
      const request = projectAgentCardGetRequest(target);
      const res = await postOp<ProjectAgentCardGetResult>(request.operation, request.data);
      if (res?.ok === false) return rejectWithValue(resultError(res, 'This Card could not be opened through the project'));
      const record = projectAgentCardRecord(res);
      if (!record) return rejectWithValue('Connection Hub returned no Card');
      return record;
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

export interface CreateDelegatedAccessArgs {
  label: string;
  resourceGrants: Record<string, string[]>;
  resourceOperations: DelegatedAccessResourceOperations;
  invocationModes: Record<string, Record<string, 'always' | 'once'>>;
  operations?: string[];
  /** `"*"` when every operation the current catalog offers for the selected
   *  resources is ticked, an exact map otherwise, {} for nothing. */
  namedServiceOperations: DelegatedAccessStoredNamedServices;
  /** Per-account binding {provider:{account_id:[claims]}}. Undefined preserves
   *  an existing binding; {} explicitly restricts the caller to no accounts. */
  accountScope?: Record<string, Record<string, string[]>>;
  properties?: Record<string, unknown>;
  ttlSeconds?: number;
}

export const createDelegatedAccess = createAsyncThunk<
  DelegatedAccessCreateResult,
  CreateDelegatedAccessArgs,
  { rejectValue: string }
>(
  'delegatedAccess/create',
  async ({ label, resourceGrants, resourceOperations, invocationModes, operations, namedServiceOperations, accountScope, properties, ttlSeconds }, { rejectWithValue }) => {
    try {
      const res = await postOp<DelegatedAccessCreateResult>('delegated_access_create', {
        label,
        resource_grants: resourceGrants || {},
        resource_operations: resourceOperations || {},
        invocation_policies: invocationModes || {},
        ...(operations !== undefined ? { operations } : {}),
        named_service_operations: namedServiceOperations || {},
        ...(accountScope !== undefined
          ? { account_scope: accountScope }
          : {}),
        ...(properties !== undefined ? { properties } : {}),
        ttl_seconds: ttlSeconds || undefined,
      });
      if (res?.ok === false) return rejectWithValue(resultError(res, 'Failed to create delegated access'));
      return res || {};
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

/** The focused-grant arguments; the wire shape lives in agentGrantPayload.ts. */
export type { GrantAgentAccessArgs };

/** Grant a hosted agent (a "Delegated By KDCube" entity) access to a resource —
 *  the consent action behind a pending agent MCP demand. Keyed to the agent's
 *  deterministic client_id, so it dedupes and appears in this list like any
 *  delegated grant. */
export const grantAgentAccess = createAsyncThunk<
  DelegatedAccessCreateResult,
  GrantAgentAccessArgs,
  { rejectValue: string }
>(
  'delegatedAccess/grantAgent',
  async (args, { rejectWithValue }) => {
    try {
      const res = await postOp<DelegatedAccessCreateResult>('delegated_agent_grant_create', agentGrantWirePayload(args));
      if (res?.ok === false) return rejectWithValue(resultError(res, 'Failed to grant agent access'));
      return res || {};
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

export interface UpdateDelegatedAccessArgs {
  accessId: string;
  label: string;
  resourceGrants: Record<string, string[]>;
  resourceOperations: DelegatedAccessResourceOperations;
  operations?: string[];
  /** Namespace narrowing {resource:{namespace:[operation]}}, or `"*"` when the
   *  operator ticked every operation the current catalog offers. Undefined
   *  preserves the record's; {} narrows every resource to nothing. */
  namedServiceOperations?: DelegatedAccessStoredNamedServices;
  /** Per-account binding {provider:{account_id:[claims]}}. Undefined preserves
   *  an existing binding; {} explicitly restricts the caller to no accounts. */
  accountScope?: Record<string, Record<string, string[]>>;
  /** What the editor loaded. The server refuses the save with 409 when either
   *  moved, so choices made against an obsolete view cannot acknowledge a
   *  newer catalog. */
  expectedCardRevision?: number;
  expectedCatalogVersion?: string;
  /** Per resource, the selected operations whose CHANGED descriptor the
   *  grantor reviewed and accepts with this save. Others stay suspended. */
  acceptedOperations?: Record<string, string[]>;
  /** Credentialless Cards use these ordinary Card fields. They are omitted
   *  for credential-bearing Cards, preserving their current values. */
  compositionMode?: 'and' | 'or';
  properties?: Record<string, unknown>;
  projectPersonControl?: ProjectControlCoordinates;
  /** W319: save another person's agent Card through the project path. */
  projectAgentCard?: ProjectAgentCardTarget;
}

/** Edit a manual automation IN PLACE — the card keeps its access_id/client_id,
 *  so the token the operator already copied stays valid; only the granted scope
 *  (and label) changes. The guard resolves the card live, so the new scope
 *  applies on the bearer's next call. No token is re-issued. */
export const updateDelegatedAccess = createAsyncThunk<
  DelegatedAccessCreateResult,
  UpdateDelegatedAccessArgs,
  { rejectValue: string }
>(
  'delegatedAccess/update',
  async (
    {
      accessId,
      label,
      resourceGrants,
      resourceOperations,
      operations,
      namedServiceOperations,
      accountScope,
      expectedCardRevision,
      expectedCatalogVersion,
      acceptedOperations,
      compositionMode,
      properties,
      projectPersonControl,
      projectAgentCard,
    },
    { rejectWithValue },
  ) => {
    try {
      const res = await postOp<DelegatedAccessCreateResult>(
        projectPersonControl
          ? 'project_person_control_update'
          : projectAgentCard
            ? 'project_agent_card_update'
            : 'delegated_access_update',
        {
          ...(projectAgentCard
            ? { access_id: projectAgentCard.accessId, project_ref: projectAgentCard.projectRef }
            : {}),
          ...(projectAgentCard ? {} : projectPersonControl
            ? projectPersonControl.kind === 'person'
              ? {
                  project_ref: projectPersonControl.projectRef,
                  target_subject: projectPersonControl.targetSubject,
                }
              : {
                  project_ref: projectPersonControl.projectRef,
                  invitation_ref: projectPersonControl.invitationRef,
                  control_id: accessId,
                }
            : { access_id: accessId }),
          label,
          resource_grants: resourceGrants || {},
          resource_operations: resourceOperations || {},
          ...(operations !== undefined ? { operations } : {}),
          ...(namedServiceOperations !== undefined
            ? { named_service_operations: namedServiceOperations }
            : {}),
          ...(accountScope !== undefined
            ? { account_scope: accountScope }
            : {}),
          ...(expectedCardRevision !== undefined
            ? { expected_card_revision: expectedCardRevision }
            : {}),
          ...(expectedCatalogVersion
            ? { expected_catalog_version: expectedCatalogVersion }
            : {}),
          ...(acceptedOperations && Object.keys(acceptedOperations).length
            ? { accepted_operations: acceptedOperations }
            : {}),
          ...(compositionMode ? { composition_mode: compositionMode } : {}),
          ...(properties !== undefined ? { properties } : {}),
        },
      );
      // A precondition failure is not an error to show and forget: it carries
      // the refreshed card the editor must reload.
      if (res?.ok === false && res?.status === 409) return res;
      if (res?.ok === false) return rejectWithValue(resultError(res, 'Failed to update delegated access'));
      return res || {};
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

export interface UpdateAgentCapabilitySelectionArgs {
  accessId: string;
  selectedCapabilities?: Record<string, unknown>;
  resetToControlDefaults?: boolean;
  expectedCardRevision: number;
}

/** Replace the visible Control-bounded selection on one resident Agent Card,
 *  or restore the current Control Card defaults. */
export const updateAgentCapabilitySelection = createAsyncThunk<
  DelegatedAccessCreateResult,
  UpdateAgentCapabilitySelectionArgs,
  { rejectValue: string }
>(
  'delegatedAccess/updateAgentCapabilitySelection',
  async (
    {
      accessId,
      selectedCapabilities,
      resetToControlDefaults,
      expectedCardRevision,
    },
    { rejectWithValue },
  ) => {
    try {
      const res = await postOp<DelegatedAccessCreateResult>('delegated_access_update', {
        access_id: accessId,
        ...(selectedCapabilities !== undefined
          ? { selected_capabilities: selectedCapabilities }
          : {}),
        ...(resetToControlDefaults ? { reset_to_control_defaults: true } : {}),
        expected_card_revision: expectedCardRevision,
      });
      if (res?.ok === false && res?.status === 409) return res;
      if (res?.ok === false) {
        return rejectWithValue(resultError(res, 'Failed to update the Agent Card base'));
      }
      return res || {};
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

export const revokeDelegatedAccess = createAsyncThunk<
  DelegatedAccessRevokeResult,
  {
    accessId: string;
    projectPersonControl?: ProjectControlCoordinates;
  },
  { rejectValue: string }
>(
  'delegatedAccess/revoke',
  async ({ accessId, projectPersonControl }, { rejectWithValue }) => {
    try {
      const res = await postOp<DelegatedAccessRevokeResult>(
        projectPersonControl ? 'project_person_control_revoke' : 'delegated_access_revoke',
        projectPersonControl
          ? projectPersonControl.kind === 'person'
            ? {
                project_ref: projectPersonControl.projectRef,
                target_subject: projectPersonControl.targetSubject,
              }
            : {
                project_ref: projectPersonControl.projectRef,
                invitation_ref: projectPersonControl.invitationRef,
                control_id: accessId,
              }
          : { access_id: accessId },
      );
      if (res?.ok === false) return rejectWithValue(resultError(res, 'Failed to revoke delegated access'));
      return res || {};
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

/** A fresh token on an existing manual card, every grant kept. The server
 *  answers like creation, so the same one-time token banner shows it. */
export type DelegatedAccessRenewMode = 'prolong' | 'reissue';

export const renewDelegatedAccess = createAsyncThunk<
  DelegatedAccessRenewResult,
  { accessId: string; mode: DelegatedAccessRenewMode },
  { rejectValue: string }
>(
  'delegatedAccess/renew',
  async ({ accessId, mode }, { rejectWithValue }) => {
    try {
      const res = await postOp<DelegatedAccessRenewResult>('delegated_access_renew', { access_id: accessId, mode });
      if (res?.ok === false) return rejectWithValue(resultError(res, 'Failed to renew delegated access'));
      return res || {};
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

export interface SetDelegatedInvocationPolicyArgs {
  accessId: string;
  resource: string;
  operation: string;
  mode: 'always' | 'once';
  expectedRevision: number;
}

export const setDelegatedInvocationPolicy = createAsyncThunk<
  DelegatedInvocationPolicyResult,
  SetDelegatedInvocationPolicyArgs,
  { rejectValue: string }
>(
  'delegatedAccess/setInvocationPolicy',
  async (
    { accessId, resource, operation, mode, expectedRevision },
    { rejectWithValue },
  ) => {
    try {
      const res = await postOp<DelegatedInvocationPolicyResult>(
        'delegated_invocation_policy_set',
        {
          access_id: accessId,
          resource,
          operation,
          mode,
          expected_revision: expectedRevision,
        },
      );
      if (res?.ok === false) {
        return rejectWithValue(resultError(res, 'Failed to update invocation policy'));
      }
      return res || {};
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

const delegatedAccessSlice = createSlice({
  name: 'delegatedAccess',
  initialState,
  reducers: {
    clearDelegatedAccessError(state) {
      state.error = '';
    },
    clearIssuedDelegatedAccess(state) {
      state.issuedToken = '';
      state.issuedHeader = '';
      state.issuedAccess = undefined;
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(loadDelegatedAccess.pending, (state, action) => {
        state.loadRequestId = action.meta.requestId;
        if (!state.items.length) state.loading = true;
      })
      .addCase(loadDelegatedAccess.fulfilled, (state, action) => {
        if (state.loadRequestId !== action.meta.requestId) return;
        state.loadRequestId = '';
        state.loading = false;
        state.platformUserId = action.payload.platform_user_id || '';
        state.items = action.payload.items || [];
        state.grantOptions = action.payload.grant_options || [];
        state.resources = action.payload.resources || [];
      })
      .addCase(loadDelegatedAccess.rejected, (state, action) => {
        if (state.loadRequestId !== action.meta.requestId) return;
        state.loadRequestId = '';
        state.loading = false;
        state.error = action.payload ?? 'Failed to load delegated access';
      })
      .addCase(loadProjectAgentCard.fulfilled, (state, action) => {
        state.focusedCard = action.payload;
      })
      .addCase(loadProjectAgentCard.rejected, (state, action) => {
        state.focusedCard = undefined;
        state.error = action.payload || 'This Card could not be opened through the project';
      })
      .addCase(loadControlCard.pending, (state) => {
        state.busy = true;
        state.error = '';
      })
      .addCase(loadControlCard.fulfilled, (state, action) => {
        state.busy = false;
        state.focusedCard = action.payload.access;
        state.focusedViewer = action.payload.viewer;
      })
      .addCase(loadControlCard.rejected, (state, action) => {
        state.busy = false;
        state.focusedCard = undefined;
        state.focusedViewer = undefined;
        state.error = action.payload ?? 'Failed to load the Control Card';
      });

    builder
      .addCase(createDelegatedAccess.pending, (state) => {
        state.busy = true;
        state.error = '';
        state.issuedToken = '';
        state.issuedHeader = '';
        state.issuedAccess = undefined;
      })
      .addCase(createDelegatedAccess.fulfilled, (state, action: PayloadAction<DelegatedAccessCreateResult>) => {
        state.busy = false;
        state.issuedToken = action.payload.access_token || '';
        state.issuedHeader = action.payload.authorization_header || '';
        state.issuedAccess = action.payload.access;
        if (action.payload.access) {
          state.items = [action.payload.access, ...state.items.filter((item) => item.access_id !== action.payload.access?.access_id)];
        }
      })
      .addCase(createDelegatedAccess.rejected, (state, action) => {
        state.busy = false;
        state.error = action.payload ?? 'Failed to create delegated access';
      })
      .addCase(grantAgentAccess.pending, (state) => {
        state.busy = true;
        state.error = '';
      })
      .addCase(grantAgentAccess.fulfilled, (state, action: PayloadAction<DelegatedAccessCreateResult>) => {
        state.busy = false;
        if (action.payload.access) {
          state.items = [action.payload.access, ...state.items.filter((item) => item.access_id !== action.payload.access?.access_id)];
        }
      })
      .addCase(grantAgentAccess.rejected, (state, action) => {
        state.busy = false;
        state.error = action.payload ?? 'Failed to grant agent access';
      })
      .addCase(updateDelegatedAccess.pending, (state) => {
        state.busy = true;
        state.error = '';
      })
      .addCase(updateDelegatedAccess.fulfilled, (state, action) => {
        state.busy = false;
        const accessId = action.meta.arg.accessId;
        if (action.payload.revoked) {
          state.items = state.items.filter((item) => item.access_id !== accessId);
          if (state.issuedAccess?.access_id === accessId) {
            state.issuedToken = '';
            state.issuedHeader = '';
            state.issuedAccess = undefined;
          }
          state.error =
            action.payload.message ||
            'Every selection on this card was withdrawn from the service catalog, so it was revoked.';
          return;
        }
        if (action.payload.status === 409) {
          // The card the server returned is the current one; the editor reopens
          // on it instead of retrying against the view it had.
          state.error =
            'This access changed while you were editing it. The latest version is shown; review and save again.';
        }
        if (action.payload.access) {
          const updated = action.payload.access;
          state.items = state.items.map((item) => (item.access_id === updated.access_id ? updated : item));
          if (state.focusedCard?.access_id === updated.access_id) {
            state.focusedCard = updated;
          }
          if (state.issuedAccess?.access_id === updated.access_id) {
            state.issuedAccess = updated;
          }
        }
      })
      .addCase(updateDelegatedAccess.rejected, (state, action) => {
        state.busy = false;
        state.error = action.payload ?? 'Failed to update delegated access';
      })
      .addCase(updateAgentCapabilitySelection.pending, (state) => {
        state.busy = true;
        state.error = '';
      })
      .addCase(updateAgentCapabilitySelection.fulfilled, (state, action) => {
        state.busy = false;
        const updated = action.payload.access;
        if (action.payload.status === 409) {
          state.error = 'This Agent Card changed while you were editing it. Review the current base and save again.';
        }
        if (updated) {
          state.items = state.items.map((item) => (
            item.access_id === updated.access_id ? updated : item
          ));
          if (state.focusedCard?.access_id === updated.access_id) {
            state.focusedCard = updated;
          }
        }
      })
      .addCase(updateAgentCapabilitySelection.rejected, (state, action) => {
        state.busy = false;
        state.error = action.payload ?? 'Failed to update the Agent Card base';
      })
      .addCase(renewDelegatedAccess.pending, (state) => {
        state.busy = true;
        state.error = '';
        state.issuedToken = '';
        state.issuedHeader = '';
        state.issuedAccess = undefined;
      })
      .addCase(renewDelegatedAccess.fulfilled, (state, action: PayloadAction<DelegatedAccessRenewResult>) => {
        state.busy = false;
        state.issuedToken = action.payload.access_token || '';
        state.issuedHeader = action.payload.authorization_header || '';
        state.issuedAccess = action.payload.access;
        const renewed = action.payload.access;
        if (renewed) {
          state.items = state.items.map((item) => (item.access_id === renewed.access_id ? renewed : item));
        }
      })
      .addCase(renewDelegatedAccess.rejected, (state, action) => {
        state.busy = false;
        state.error = action.payload ?? 'Failed to renew delegated access';
      })
      .addCase(revokeDelegatedAccess.pending, (state) => {
        state.busy = true;
        state.error = '';
      })
      .addCase(revokeDelegatedAccess.fulfilled, (state, action) => {
        state.busy = false;
        const id = action.meta.arg.accessId;
        state.items = state.items.filter((item) => item.access_id !== id);
        if (state.focusedCard?.access_id === id) state.focusedCard = undefined;
        if (state.issuedAccess?.access_id === id) {
          state.issuedToken = '';
          state.issuedHeader = '';
          state.issuedAccess = undefined;
        }
      })
      .addCase(revokeDelegatedAccess.rejected, (state, action) => {
        state.busy = false;
        state.error = action.payload ?? 'Failed to revoke delegated access';
      })
      .addCase(setDelegatedInvocationPolicy.pending, (state) => {
        state.busy = true;
        state.error = '';
      })
      .addCase(setDelegatedInvocationPolicy.fulfilled, (state, action) => {
        state.busy = false;
        const policy = action.payload.policy;
        if (!policy) return;
        state.items = state.items.map((item) => {
          if (item.access_id !== policy.authority.access_id) return item;
          const policies = (item.invocation_policies || []).filter(
            (existing) => existing.policy_id !== policy.policy_id,
          );
          return { ...item, invocation_policies: [...policies, policy] };
        });
      })
      .addCase(setDelegatedInvocationPolicy.rejected, (state, action) => {
        state.busy = false;
        state.error = action.payload ?? 'Failed to update invocation policy';
      });
  },
});

export const { clearDelegatedAccessError, clearIssuedDelegatedAccess } = delegatedAccessSlice.actions;
export default delegatedAccessSlice.reducer;
