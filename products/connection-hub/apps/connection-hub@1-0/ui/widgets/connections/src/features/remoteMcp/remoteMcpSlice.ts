import { createAsyncThunk, createSlice, type PayloadAction } from '@reduxjs/toolkit';
import { getOp, postOp } from '../../api/client';
import type {
  RemoteMcpConnector,
  RemoteMcpConnectorMutationResult,
  RemoteMcpConnectorsResult,
  RemoteMcpOAuthStartResult,
} from '../../api/types';

export interface RemoteMcpState {
  items: RemoteMcpConnector[];
  loading: boolean;
  busy: boolean;
  error: string;
}

const initialState: RemoteMcpState = {
  items: [],
  loading: true,
  busy: false,
  error: '',
};

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function resultError(
  result: { error?: string; message?: string } | null | undefined,
  fallback: string,
): string {
  return result?.message || result?.error || fallback;
}

export const loadRemoteMcpConnectors = createAsyncThunk<
  RemoteMcpConnector[],
  void,
  { rejectValue: string }
>('remoteMcp/load', async (_arg, { rejectWithValue }) => {
  try {
    const result = await getOp<RemoteMcpConnectorsResult>('remote_mcp_connectors_list');
    if (result?.ok === false) return rejectWithValue(resultError(result, 'Failed to load MCP connectors'));
    return Array.isArray(result?.items) ? result.items : [];
  } catch (error) {
    return rejectWithValue(message(error));
  }
});

export interface CreateRemoteMcpArgs {
  label: string;
  endpoint: string;
  credentialMode: 'none' | 'bearer' | 'header';
  credentialHeader?: string;
  credentialValue?: string;
}

export interface StartRemoteMcpOAuthArgs {
  label: string;
  endpoint: string;
  returnHint?: string;
  connectorId?: string;
  expectedRevision?: number;
  oauthClientMode?: 'automatic' | 'provisioned';
  oauthClient?: {
    clientId: string;
    clientSecret?: string;
    tokenEndpointAuthMethod: 'none' | 'client_secret_basic' | 'client_secret_post';
  };
}

/** Start upstream OAuth without retaining provider client credentials in Redux. */
export async function requestRemoteMcpOAuth(
  args: StartRemoteMcpOAuthArgs,
): Promise<RemoteMcpOAuthStartResult> {
  const payload: Record<string, unknown> = {
    label: args.label,
    endpoint: args.endpoint,
    return_hint: args.returnHint || '',
    connector_id: args.connectorId || '',
    expected_revision: args.expectedRevision || 0,
  };
  if (args.oauthClientMode) payload.oauth_client_mode = args.oauthClientMode;
  if (args.oauthClient) {
    payload.oauth_client = {
      client_id: args.oauthClient.clientId,
      client_secret: args.oauthClient.clientSecret || '',
      token_endpoint_auth_method: args.oauthClient.tokenEndpointAuthMethod,
    };
  }
  const result = await postOp<RemoteMcpOAuthStartResult>(
    'remote_mcp_connector_start_oauth',
    payload,
  );
  if (result?.ok === false || !result?.authorize_url) {
    throw new Error(resultError(result, 'Failed to start MCP authorization'));
  }
  return result;
}

export const startRemoteMcpOAuth = createAsyncThunk<
  RemoteMcpOAuthStartResult,
  Omit<StartRemoteMcpOAuthArgs, 'oauthClient'>,
  { rejectValue: string }
>('remoteMcp/startOAuth', async (args, { rejectWithValue }) => {
  try {
    return await requestRemoteMcpOAuth(args);
  } catch (error) {
    return rejectWithValue(message(error));
  }
});

export const createRemoteMcpConnector = createAsyncThunk<
  RemoteMcpConnector,
  CreateRemoteMcpArgs,
  { rejectValue: string }
>('remoteMcp/create', async (args, { rejectWithValue }) => {
  try {
    const result = await postOp<RemoteMcpConnectorMutationResult>('remote_mcp_connector_create', {
      label: args.label,
      endpoint: args.endpoint,
      credential_mode: args.credentialMode,
      credential_header: args.credentialHeader || '',
      credential_value: args.credentialValue || '',
    });
    if (result?.ok === false || !result?.connector) {
      return rejectWithValue(resultError(result, 'Failed to connect the MCP server'));
    }
    return result.connector;
  } catch (error) {
    return rejectWithValue(message(error));
  }
});

type RevisionArgs = { connectorId: string; expectedRevision: number };

function connectorMutation(
  type: string,
  operation: string,
  extra: (args: RevisionArgs & Record<string, unknown>) => Record<string, unknown> = () => ({}),
) {
  return createAsyncThunk<
    RemoteMcpConnector,
    RevisionArgs & Record<string, unknown>,
    { rejectValue: string }
  >(type, async (args, { rejectWithValue }) => {
    try {
      const result = await postOp<RemoteMcpConnectorMutationResult>(operation, {
        connector_id: args.connectorId,
        expected_revision: args.expectedRevision,
        ...extra(args),
      });
      if (result?.ok === false || !result?.connector) {
        return rejectWithValue(resultError(result, `${operation} failed`));
      }
      return result.connector;
    } catch (error) {
      return rejectWithValue(message(error));
    }
  });
}

export const refreshRemoteMcpConnector = connectorMutation(
  'remoteMcp/refresh',
  'remote_mcp_connector_refresh',
);

export const acceptRemoteMcpDescriptor = connectorMutation(
  'remoteMcp/acceptDescriptor',
  'remote_mcp_connector_accept_descriptor',
);

export const setRemoteMcpConnectorEnabled = connectorMutation(
  'remoteMcp/setEnabled',
  'remote_mcp_connector_set_enabled',
  (args) => ({ enabled: Boolean(args.enabled) }),
);

export const updateRemoteMcpCredential = connectorMutation(
  'remoteMcp/updateCredential',
  'remote_mcp_connector_update_credential',
  (args) => ({
    credential_mode: args.credentialMode,
    credential_header: args.credentialHeader || '',
    credential_value: args.credentialValue || '',
  }),
);

export const deleteRemoteMcpConnector = createAsyncThunk<
  { connectorId: string },
  RevisionArgs,
  { rejectValue: string }
>('remoteMcp/delete', async (args, { rejectWithValue }) => {
  try {
    const result = await postOp<RemoteMcpConnectorMutationResult>('remote_mcp_connector_delete', {
      connector_id: args.connectorId,
      expected_revision: args.expectedRevision,
    });
    if (result?.ok === false || !result?.removed) {
      return rejectWithValue(resultError(result, 'Failed to remove the MCP connector'));
    }
    return { connectorId: args.connectorId };
  } catch (error) {
    return rejectWithValue(message(error));
  }
});

const mutations = [
  createRemoteMcpConnector,
  refreshRemoteMcpConnector,
  acceptRemoteMcpDescriptor,
  setRemoteMcpConnectorEnabled,
  updateRemoteMcpCredential,
] as const;

const remoteMcpSlice = createSlice({
  name: 'remoteMcp',
  initialState,
  reducers: {
    clearRemoteMcpError(state) {
      state.error = '';
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(loadRemoteMcpConnectors.fulfilled, (state, action: PayloadAction<RemoteMcpConnector[]>) => {
        state.loading = false;
        state.items = action.payload;
      })
      .addCase(loadRemoteMcpConnectors.rejected, (state, action) => {
        state.loading = false;
        state.error = action.payload ?? 'Failed to load MCP connectors';
      })
      .addCase(deleteRemoteMcpConnector.fulfilled, (state, action) => {
        state.busy = false;
        state.items = state.items.filter((item) => item.connector_id !== action.payload.connectorId);
      });

    mutations.forEach((thunk) => {
      builder
        .addCase(thunk.pending, (state) => {
          state.busy = true;
          state.error = '';
        })
        .addCase(thunk.fulfilled, (state, action) => {
          state.busy = false;
          const index = state.items.findIndex((item) => item.connector_id === action.payload.connector_id);
          if (index >= 0) state.items[index] = action.payload;
          else state.items.push(action.payload);
        })
        .addCase(thunk.rejected, (state, action) => {
          state.busy = false;
          state.error = action.payload ?? 'MCP connector operation failed';
        });
    });
    builder
      .addCase(deleteRemoteMcpConnector.pending, (state) => {
        state.busy = true;
        state.error = '';
      })
      .addCase(deleteRemoteMcpConnector.rejected, (state, action) => {
        state.busy = false;
        state.error = action.payload ?? 'Failed to remove the MCP connector';
      })
      .addCase(startRemoteMcpOAuth.pending, (state) => {
        state.busy = true;
        state.error = '';
      })
      .addCase(startRemoteMcpOAuth.fulfilled, (state) => {
        state.busy = false;
      })
      .addCase(startRemoteMcpOAuth.rejected, (state, action) => {
        state.busy = false;
        state.error = action.payload ?? 'Failed to start MCP authorization';
      });
  },
});

export const { clearRemoteMcpError } = remoteMcpSlice.actions;
export default remoteMcpSlice.reducer;
