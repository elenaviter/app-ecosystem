/**
 * The exact wire payload of `delegated_agent_grant_create`, built in one place
 * so a test can pin what the widget submits for a focused grant (one operation
 * with its invocation policy) without a browser or a store. The thunk in
 * `delegatedAccessSlice.ts` sends this object verbatim.
 */

export interface GrantAgentAccessArgs {
  clientId: string;
  resource: string;
  claims: string[];
  /** The exact existing card a denial is recovered on. */
  accessId?: string;
  /** Repeated or single use for the ONE outer operation carried in
   *  `resourceOperations`; the server refuses a mode with any other count. */
  invocationMode?: 'always' | 'once';
  /** Identity of this policy change: the denial's invocation id, or an id the
   *  editor minted. The server commits it exactly once and replays on retry. */
  invocationChangeId?: string;
  requestBound?: boolean;
  requestDigest?: string;
  requestApprovalTicket?: string;
  requestCardRevision?: number;
  requestAuthorityRevision?: string;
  approvalContext?: Record<string, string>;
  label?: string;
  /** Exact outer operation selection for THIS resource. */
  resourceOperations?: string[];
  /** Named-service narrowing for THIS resource (namespace -> exact operations). */
  namedServiceOperations?: Record<string, string[]>;
  /** EDIT semantics: the submitted claim set REPLACES the record exactly. */
  replace?: boolean;
  /** Per-account claim binding: {provider_id: {account_id: [claims]}}. */
  accountScope?: Record<string, Record<string, string[]>>;
}

export function agentGrantWirePayload(args: GrantAgentAccessArgs): Record<string, unknown> {
  const {
    clientId, resource, claims, accessId, invocationMode, invocationChangeId, requestBound,
    requestDigest, requestApprovalTicket, requestCardRevision, requestAuthorityRevision,
    approvalContext, label, resourceOperations, namedServiceOperations, replace, accountScope,
  } = args;
  return {
    client_id: clientId,
    resource,
    claims: claims || [],
    label: label || '',
    ...(accessId ? { access_id: accessId } : {}),
    ...(invocationMode ? { invocation_mode: invocationMode } : {}),
    ...(invocationChangeId ? { invocation_change_id: invocationChangeId } : {}),
    ...(requestBound ? { request_bound: true } : {}),
    ...(requestDigest ? { request_digest: requestDigest } : {}),
    ...(requestApprovalTicket ? { request_approval_ticket: requestApprovalTicket } : {}),
    ...(requestCardRevision ? { request_card_revision: requestCardRevision } : {}),
    ...(requestAuthorityRevision ? { request_authority_revision: requestAuthorityRevision } : {}),
    ...(approvalContext && Object.keys(approvalContext).length ? { approval_context: approvalContext } : {}),
    ...(replace ? { replace: true } : {}),
    ...(resourceOperations !== undefined ? { resource_operations: { [resource]: resourceOperations } } : {}),
    ...(namedServiceOperations && Object.keys(namedServiceOperations).length
      ? { named_service_operations: namedServiceOperations }
      : {}),
    ...(accountScope !== undefined ? { account_scope: accountScope } : {}),
  };
}
