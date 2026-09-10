import { createAsyncThunk, createSlice, type PayloadAction } from '@reduxjs/toolkit';
import { getOp, postOp } from '../../api/client';
import type {
  AuthoritiesDescribeResult,
  AuthorityEditResult,
  AuthorityProviderValidateResult,
  AuthenticatorMutationResult,
  AuthenticatorRow,
  AuthenticatorsListResult,
  SupportedAuthenticatorProvider,
} from '../../api/types';

export interface AuthenticatorsState {
  items: AuthenticatorRow[];
  supportedProviders: SupportedAuthenticatorProvider[];
  loading: boolean;
  busy: boolean;
  error: string;
  // Operator surface: false when the backend answered platform_admin_required.
  allowed: boolean;
  authorities: AuthoritiesDescribeResult | null;
  authoritiesError: string;
  authorityEdit: AuthorityEditResult | null;
  authorityEditError: string;
  authorityEditBusy: boolean;
}

const initialState: AuthenticatorsState = {
  items: [],
  supportedProviders: [],
  loading: true,
  busy: false,
  error: '',
  allowed: true,
  authorities: null,
  authoritiesError: '',
  authorityEdit: null,
  authorityEditError: '',
  authorityEditBusy: false,
};

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export const loadAuthenticators = createAsyncThunk<AuthenticatorsListResult, void, { rejectValue: string }>(
  'authenticators/load',
  async (_arg, { rejectWithValue }) => {
    try {
      return await getOp<AuthenticatorsListResult>('authenticators_list');
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

export interface UpsertAuthenticatorArgs {
  authenticatorId: string;
  provider: string;
  authorityId?: string;
  label?: string;
  enabled?: boolean;
  roleProviding?: boolean;
  subjectNamespace?: string;
  secretRef?: string;
  selector?: Record<string, unknown>;
  verifier?: Record<string, unknown>;
  properties?: Record<string, unknown>;
}

/** The deployment's sign-in authorities: the platform's selection, the
 *  providers registry with its trusted pools, and what apps registered. */
export const loadAuthorities = createAsyncThunk<AuthoritiesDescribeResult, void, { rejectValue: string }>(
  'authenticators/authorities',
  async (_, { rejectWithValue }) => {
    try {
      const res = await getOp<AuthoritiesDescribeResult>('authorities_describe');
      if (res?.ok === false) return rejectWithValue(res.message || res.error || 'Failed to read the authorities');
      return res || {};
    } catch (e) {
      return rejectWithValue(e instanceof Error ? e.message : String(e));
    }
  },
);

/** The editing buffer checked on the server before anything is applied. */
export const validateAuthorityProvider = createAsyncThunk<
  AuthorityProviderValidateResult,
  { authorityId: string; providerId: string; yaml: string },
  { rejectValue: string }
>(
  'authenticators/authorityValidate',
  async ({ authorityId, providerId, yaml }, { rejectWithValue }) => {
    try {
      const res = await postOp<AuthorityProviderValidateResult>('authority_provider_validate', {
        authority_id: authorityId, provider_id: providerId, yaml,
      });
      return res || {};
    } catch (e) {
      return rejectWithValue(e instanceof Error ? e.message : String(e));
    }
  },
);

/** The buffer applied to the staged bundles.yaml through the platform's editor. */
export const setAuthorityProvider = createAsyncThunk<
  AuthorityEditResult,
  { authorityId: string; providerId: string; yaml: string; allowSecretRemoval?: boolean; create?: boolean },
  { rejectValue: string }
>(
  'authenticators/authoritySet',
  async ({ authorityId, providerId, yaml, allowSecretRemoval, create }, { rejectWithValue }) => {
    try {
      const res = await postOp<AuthorityEditResult>('authority_provider_set', {
        authority_id: authorityId,
        provider_id: providerId,
        yaml,
        allow_secret_removal: Boolean(allowSecretRemoval),
        create: Boolean(create),
      });
      if (res?.ok === false) return rejectWithValue([res.message || res.error || 'The edit was refused', ...(res.problems || [])].join(' '));
      return res || {};
    } catch (e) {
      return rejectWithValue(e instanceof Error ? e.message : String(e));
    }
  },
);

/** The platform's sign-in provider changed in the staged assembly.yaml. */
export const setPlatformSignIn = createAsyncThunk<AuthorityEditResult, { providerId: string }, { rejectValue: string }>(
  'authenticators/platformSignInSet',
  async ({ providerId }, { rejectWithValue }) => {
    try {
      const res = await postOp<AuthorityEditResult>('platform_sign_in_set', { provider_id: providerId });
      if (res?.ok === false) return rejectWithValue([res.message || res.error || 'The switch was refused', ...(res.problems || [])].join(' '));
      return res || {};
    } catch (e) {
      return rejectWithValue(e instanceof Error ? e.message : String(e));
    }
  },
);

export const upsertAuthenticator = createAsyncThunk<
  AuthenticatorMutationResult,
  UpsertAuthenticatorArgs,
  { rejectValue: string }
>(
  'authenticators/upsert',
  async (args, { rejectWithValue }) => {
    try {
      const res = await postOp<AuthenticatorMutationResult>('authenticators_upsert', {
        authenticator_id: args.authenticatorId,
        provider: args.provider,
        authority_id: args.authorityId || '',
        label: args.label || '',
        enabled: args.enabled !== false,
        role_providing: args.roleProviding === true,
        subject_namespace: args.subjectNamespace || '',
        secret_ref: args.secretRef || '',
        selector: args.selector || {},
        verifier: args.verifier || {},
        properties: args.properties || {},
      });
      if (res && res.ok === false) {
        return rejectWithValue(res.message || res.error || 'Authenticator save failed');
      }
      return res;
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

export const removeAuthenticator = createAsyncThunk<
  AuthenticatorMutationResult,
  string,
  { rejectValue: string }
>(
  'authenticators/remove',
  async (authenticatorId, { rejectWithValue }) => {
    try {
      const res = await postOp<AuthenticatorMutationResult>('authenticators_remove', {
        authenticator_id: authenticatorId,
      });
      if (res && res.ok === false) {
        return rejectWithValue(res.message || res.error || 'Authenticator remove failed');
      }
      return res;
    } catch (e) {
      return rejectWithValue(message(e));
    }
  },
);

const authenticatorsSlice = createSlice({
  name: 'authenticators',
  initialState,
  reducers: {
    clearAuthenticatorsError(state) {
      state.error = '';
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(setAuthorityProvider.pending, (state) => { state.authorityEditBusy = true; state.authorityEditError = ''; })
      .addCase(setAuthorityProvider.fulfilled, (state, action) => {
        state.authorityEditBusy = false;
        state.authorityEdit = action.payload;
        if (action.payload.authorities || action.payload.platform) state.authorities = action.payload;
      })
      .addCase(setAuthorityProvider.rejected, (state, action) => {
        state.authorityEditBusy = false;
        state.authorityEditError = action.payload ?? 'The edit was refused';
      })
      .addCase(setPlatformSignIn.pending, (state) => { state.authorityEditBusy = true; state.authorityEditError = ''; })
      .addCase(setPlatformSignIn.fulfilled, (state, action) => {
        state.authorityEditBusy = false;
        state.authorityEdit = action.payload;
        if (action.payload.authorities || action.payload.platform) state.authorities = action.payload;
      })
      .addCase(setPlatformSignIn.rejected, (state, action) => {
        state.authorityEditBusy = false;
        state.authorityEditError = action.payload ?? 'The switch was refused';
      })
      .addCase(loadAuthorities.fulfilled, (state, action) => {
        state.authorities = action.payload;
        state.authoritiesError = '';
      })
      .addCase(loadAuthorities.rejected, (state, action) => {
        state.authoritiesError = action.payload ?? 'Failed to read the authorities';
      })
      .addCase(loadAuthenticators.fulfilled, (state, action: PayloadAction<AuthenticatorsListResult>) => {
        state.loading = false;
        if (action.payload.ok === false && action.payload.error === 'platform_admin_required') {
          state.allowed = false;
          state.items = [];
          state.supportedProviders = [];
          return;
        }
        state.allowed = true;
        state.items = Array.isArray(action.payload.items) ? action.payload.items : [];
        state.supportedProviders = Array.isArray(action.payload.supported_providers)
          ? action.payload.supported_providers
          : [];
      })
      .addCase(loadAuthenticators.rejected, (state, action) => {
        state.loading = false;
        state.error = action.payload ?? 'Failed to load authenticators';
      });

    [upsertAuthenticator, removeAuthenticator].forEach((thunk) => {
      builder
        .addCase(thunk.pending, (state) => {
          state.busy = true;
          state.error = '';
        })
        .addCase(thunk.fulfilled, (state) => {
          state.busy = false;
        })
        .addCase(thunk.rejected, (state, action) => {
          state.busy = false;
          state.error = (action.payload as string) ?? 'Authenticator operation failed';
        });
    });
  },
});

export const { clearAuthenticatorsError } = authenticatorsSlice.actions;
export default authenticatorsSlice.reducer;
