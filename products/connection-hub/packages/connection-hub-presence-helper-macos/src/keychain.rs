use std::ptr;

use core_foundation::base::{CFType, TCFType};
use core_foundation::boolean::CFBoolean;
use core_foundation::data::CFData;
use core_foundation::dictionary::CFDictionary;
use core_foundation::string::CFString;
use core_foundation_sys::base::{CFGetTypeID, CFRelease, CFTypeRef, OSStatus};
use core_foundation_sys::string::CFStringRef;
use security_framework::access_control::{ProtectionMode, SecAccessControl};
use security_framework_sys::access_control::kSecAccessControlUserPresence;
use security_framework_sys::base::{errSecItemNotFound, errSecSuccess};
use security_framework_sys::item::{
    kSecAttrAccessControl, kSecAttrAccessGroup, kSecAttrAccount, kSecAttrService, kSecClass,
    kSecClassGenericPassword, kSecMatchLimit, kSecMatchLimitAll, kSecReturnData,
    kSecUseDataProtectionKeychain, kSecValueData,
};
use security_framework_sys::keychain_item::{
    SecItemAdd, SecItemCopyMatching, SecItemDelete, SecItemUpdate,
};
use uuid::Uuid;
use zeroize::Zeroizing;

use crate::error::{CoreError, CoreResult, ErrorCode};
use crate::service::SessionStore;
use crate::session::{AuthorizedSession, ProtectedOAuthSession, SessionDescriptor};

const PROTECTED_SERVICE: &str = "tech.kdcube.connection-hub.presence-helper.session";
const DESCRIPTOR_SERVICE: &str = "tech.kdcube.connection-hub.presence-helper.descriptor";
const MAX_PROTECTED_RECORD_BYTES: usize = 128 * 1024;
const MAX_DESCRIPTOR_RECORD_BYTES: usize = 32 * 1024;

const ERR_SEC_USER_CANCELED: OSStatus = -128;
const ERR_SEC_NOT_AVAILABLE: OSStatus = -25_291;
const ERR_SEC_INTERACTION_NOT_ALLOWED: OSStatus = -25_308;
const ERR_SEC_MISSING_ENTITLEMENT: OSStatus = -34_018;

#[link(name = "Security", kind = "framework")]
extern "C" {
    static kSecUseOperationPrompt: CFStringRef;
}

pub struct KeychainSessionStore {
    access_group: Option<&'static str>,
}

impl KeychainSessionStore {
    pub fn new() -> Self {
        let configured = env!("KDCUBE_KEYCHAIN_ACCESS_GROUP");
        Self {
            access_group: (!configured.is_empty()).then_some(configured),
        }
    }

    fn base_query(&self, service: &str, session_id: Uuid) -> Vec<(CFString, CFType)> {
        let mut query = vec![
            pair(unsafe { kSecClass }, unsafe { kSecClassGenericPassword }),
            string_pair(unsafe { kSecAttrService }, service),
            string_pair(
                unsafe { kSecAttrAccount },
                &session_id.hyphenated().to_string(),
            ),
        ];
        query.push(boolean_pair(unsafe { kSecUseDataProtectionKeychain }, true));
        if let Some(group) = self.access_group {
            query.push(string_pair(unsafe { kSecAttrAccessGroup }, group));
        }
        query
    }

    fn add_descriptor(&self, descriptor: &SessionDescriptor) -> CoreResult<()> {
        let data = serde_json::to_vec(descriptor)
            .map_err(|_| CoreError::new(ErrorCode::InternalFailure))?;
        if data.len() > MAX_DESCRIPTOR_RECORD_BYTES {
            return Err(CoreError::new(ErrorCode::InternalFailure));
        }
        let mut query = self.base_query(DESCRIPTOR_SERVICE, descriptor.session_id);
        query.push(data_pair(unsafe { kSecValueData }, &data));
        let query = dictionary(&query);
        status(
            unsafe { SecItemAdd(query.as_concrete_TypeRef(), ptr::null_mut()) },
            ErrorCode::SessionNotFound,
        )
    }

    fn add_protected(&self, session: &ProtectedOAuthSession) -> CoreResult<()> {
        let data = Zeroizing::new(
            serde_json::to_vec(session).map_err(|_| CoreError::new(ErrorCode::InternalFailure))?,
        );
        if data.len() > MAX_PROTECTED_RECORD_BYTES {
            return Err(CoreError::new(ErrorCode::InternalFailure));
        }
        let access = SecAccessControl::create_with_protection(
            Some(ProtectionMode::AccessibleWhenPasscodeSetThisDeviceOnly),
            kSecAccessControlUserPresence,
        )
        .map_err(|_| CoreError::new(ErrorCode::UserPresenceUnavailable))?;
        let mut query = self.base_query(PROTECTED_SERVICE, session.session_id);
        query.push((
            cf_string(unsafe { kSecAttrAccessControl }),
            access.as_CFType(),
        ));
        query.push(data_pair(unsafe { kSecValueData }, &data));
        let query = dictionary(&query);
        status(
            unsafe { SecItemAdd(query.as_concrete_TypeRef(), ptr::null_mut()) },
            ErrorCode::SessionNotFound,
        )
    }

    fn delete_descriptor(&self, session_id: Uuid) -> CoreResult<()> {
        let query = self.base_query(DESCRIPTOR_SERVICE, session_id);
        let query = dictionary(&query);
        let value = unsafe { SecItemDelete(query.as_concrete_TypeRef()) };
        if value == errSecSuccess || value == errSecItemNotFound {
            Ok(())
        } else {
            Err(keychain_error(value, ErrorCode::SessionNotFound))
        }
    }

    fn read_data(
        &self,
        service: &str,
        session_id: Uuid,
        prompt: Option<&str>,
        maximum: usize,
    ) -> CoreResult<Zeroizing<Vec<u8>>> {
        let mut query = self.base_query(service, session_id);
        query.push(boolean_pair(unsafe { kSecReturnData }, true));
        if let Some(prompt) = prompt {
            query.push(string_pair(unsafe { kSecUseOperationPrompt }, prompt));
        }
        let query = dictionary(&query);
        let mut result: CFTypeRef = ptr::null();
        let value = unsafe { SecItemCopyMatching(query.as_concrete_TypeRef(), &mut result) };
        if value != errSecSuccess || result.is_null() {
            return Err(keychain_error(value, ErrorCode::SessionNotFound));
        }
        let copied = copy_cf_data(result, maximum);
        unsafe { CFRelease(result) };
        copied
    }

    fn all_descriptor_data(&self) -> CoreResult<Vec<Zeroizing<Vec<u8>>>> {
        let mut query = vec![
            pair(unsafe { kSecClass }, unsafe { kSecClassGenericPassword }),
            string_pair(unsafe { kSecAttrService }, DESCRIPTOR_SERVICE),
            boolean_pair(unsafe { kSecReturnData }, true),
            pair(unsafe { kSecMatchLimit }, unsafe { kSecMatchLimitAll }),
        ];
        query.push(boolean_pair(unsafe { kSecUseDataProtectionKeychain }, true));
        if let Some(group) = self.access_group {
            query.push(string_pair(unsafe { kSecAttrAccessGroup }, group));
        }
        let query = dictionary(&query);
        let mut result: CFTypeRef = ptr::null();
        let value = unsafe { SecItemCopyMatching(query.as_concrete_TypeRef(), &mut result) };
        if value == errSecItemNotFound {
            return Ok(Vec::new());
        }
        if value != errSecSuccess || result.is_null() {
            return Err(keychain_error(value, ErrorCode::SessionNotFound));
        }
        let copied = copy_cf_data_collection(result, MAX_DESCRIPTOR_RECORD_BYTES);
        unsafe { CFRelease(result) };
        copied
    }
}

impl Default for KeychainSessionStore {
    fn default() -> Self {
        Self::new()
    }
}

impl SessionStore for KeychainSessionStore {
    fn create(&self, value: &AuthorizedSession) -> CoreResult<()> {
        self.add_descriptor(&value.descriptor)?;
        if let Err(error) = self.add_protected(&value.session) {
            let _ = self.delete_descriptor(value.descriptor.session_id);
            return Err(error);
        }
        Ok(())
    }

    fn list_session_ids(&self) -> CoreResult<Vec<Uuid>> {
        let mut values = Vec::new();
        for data in self.all_descriptor_data()? {
            let descriptor: SessionDescriptor = serde_json::from_slice(&data)
                .map_err(|_| CoreError::new(ErrorCode::SessionReauthorizationRequired))?;
            values.push(descriptor.session_id);
        }
        values.sort_unstable();
        values.dedup();
        Ok(values)
    }

    fn describe(&self, session_id: Uuid) -> CoreResult<SessionDescriptor> {
        let data = self.read_data(
            DESCRIPTOR_SERVICE,
            session_id,
            None,
            MAX_DESCRIPTOR_RECORD_BYTES,
        )?;
        serde_json::from_slice(&data)
            .map_err(|_| CoreError::new(ErrorCode::SessionReauthorizationRequired))
    }

    fn read(&self, session_id: Uuid, prompt: &str) -> CoreResult<ProtectedOAuthSession> {
        let data = self.read_data(
            PROTECTED_SERVICE,
            session_id,
            Some(prompt),
            MAX_PROTECTED_RECORD_BYTES,
        )?;
        serde_json::from_slice(&data)
            .map_err(|_| CoreError::new(ErrorCode::SessionReauthorizationRequired))
    }

    fn replace(
        &self,
        session_id: Uuid,
        expected_generation: u64,
        value: &ProtectedOAuthSession,
    ) -> CoreResult<()> {
        if value.session_id != session_id || value.generation != expected_generation + 1 {
            return Err(CoreError::new(ErrorCode::SessionReauthorizationRequired));
        }
        let data = Zeroizing::new(
            serde_json::to_vec(value).map_err(|_| CoreError::new(ErrorCode::InternalFailure))?,
        );
        if data.len() > MAX_PROTECTED_RECORD_BYTES {
            return Err(CoreError::new(ErrorCode::InternalFailure));
        }
        let mut query = self.base_query(PROTECTED_SERVICE, session_id);
        query.push(string_pair(
            unsafe { kSecUseOperationPrompt },
            "Update the protected Connection Hub session after token rotation.",
        ));
        let attributes = vec![data_pair(unsafe { kSecValueData }, &data)];
        let query = dictionary(&query);
        let attributes = dictionary(&attributes);
        status(
            unsafe {
                SecItemUpdate(
                    query.as_concrete_TypeRef(),
                    attributes.as_concrete_TypeRef(),
                )
            },
            ErrorCode::SessionNotFound,
        )
    }

    fn require_reauthorization(&self, session_id: Uuid) -> CoreResult<()> {
        let mut descriptor = self.describe(session_id)?;
        descriptor.reauthorization_required = true;
        let data = serde_json::to_vec(&descriptor)
            .map_err(|_| CoreError::new(ErrorCode::InternalFailure))?;
        let query = self.base_query(DESCRIPTOR_SERVICE, session_id);
        let attributes = vec![data_pair(unsafe { kSecValueData }, &data)];
        let query = dictionary(&query);
        let attributes = dictionary(&attributes);
        status(
            unsafe {
                SecItemUpdate(
                    query.as_concrete_TypeRef(),
                    attributes.as_concrete_TypeRef(),
                )
            },
            ErrorCode::SessionNotFound,
        )
    }

    fn remove(&self, session_id: Uuid, prompt: &str) -> CoreResult<bool> {
        let mut query = self.base_query(PROTECTED_SERVICE, session_id);
        query.push(string_pair(unsafe { kSecUseOperationPrompt }, prompt));
        let query = dictionary(&query);
        let protected_status = unsafe { SecItemDelete(query.as_concrete_TypeRef()) };
        if protected_status == errSecItemNotFound {
            self.delete_descriptor(session_id)?;
            return Ok(false);
        }
        if protected_status != errSecSuccess {
            return Err(keychain_error(protected_status, ErrorCode::SessionNotFound));
        }
        self.delete_descriptor(session_id)?;
        Ok(true)
    }
}

fn dictionary(pairs: &[(CFString, CFType)]) -> CFDictionary<CFString, CFType> {
    CFDictionary::from_CFType_pairs(pairs)
}

fn pair(key: CFStringRef, value: CFStringRef) -> (CFString, CFType) {
    (cf_string(key), cf_string(value).into_CFType())
}

fn string_pair(key: CFStringRef, value: &str) -> (CFString, CFType) {
    (cf_string(key), CFString::new(value).into_CFType())
}

fn boolean_pair(key: CFStringRef, value: bool) -> (CFString, CFType) {
    (cf_string(key), CFBoolean::from(value).into_CFType())
}

fn data_pair(key: CFStringRef, value: &[u8]) -> (CFString, CFType) {
    (cf_string(key), CFData::from_buffer(value).into_CFType())
}

fn cf_string(value: CFStringRef) -> CFString {
    unsafe { CFString::wrap_under_get_rule(value) }
}

fn copy_cf_data(result: CFTypeRef, maximum: usize) -> CoreResult<Zeroizing<Vec<u8>>> {
    if unsafe { CFGetTypeID(result) } != CFData::type_id() {
        return Err(CoreError::new(ErrorCode::InternalFailure));
    }
    let data = unsafe { CFData::wrap_under_get_rule(result.cast()) };
    if data.len() < 0 || data.len() as usize > maximum {
        return Err(CoreError::new(ErrorCode::ResponseTooLarge));
    }
    Ok(Zeroizing::new(data.bytes().to_vec()))
}

fn copy_cf_data_collection(
    result: CFTypeRef,
    maximum: usize,
) -> CoreResult<Vec<Zeroizing<Vec<u8>>>> {
    if unsafe { CFGetTypeID(result) } == CFData::type_id() {
        return Ok(vec![copy_cf_data(result, maximum)?]);
    }
    if unsafe { CFGetTypeID(result) } != core_foundation::array::CFArray::<CFType>::type_id() {
        return Err(CoreError::new(ErrorCode::InternalFailure));
    }
    let array =
        unsafe { core_foundation::array::CFArray::<CFType>::wrap_under_get_rule(result.cast()) };
    if array.len() > 1024 {
        return Err(CoreError::new(ErrorCode::InternalFailure));
    }
    array
        .iter()
        .map(|item| copy_cf_data(item.as_CFTypeRef(), maximum))
        .collect()
}

fn status(value: OSStatus, missing: ErrorCode) -> CoreResult<()> {
    if value == errSecSuccess {
        Ok(())
    } else {
        Err(keychain_error(value, missing))
    }
}

fn keychain_error(value: OSStatus, missing: ErrorCode) -> CoreError {
    if value == ERR_SEC_MISSING_ENTITLEMENT {
        CoreError::new(ErrorCode::HelperSigningInvalid)
    } else if value == ERR_SEC_USER_CANCELED {
        CoreError::new(ErrorCode::UserPresenceCancelled)
    } else if matches!(
        value,
        ERR_SEC_INTERACTION_NOT_ALLOWED | ERR_SEC_NOT_AVAILABLE
    ) {
        CoreError::new(ErrorCode::UserPresenceUnavailable)
    } else if value == errSecItemNotFound {
        CoreError::new(missing)
    } else {
        CoreError::new(ErrorCode::InternalFailure)
    }
}

#[cfg(test)]
mod tests {
    use core_foundation::base::TCFType;
    use core_foundation::data::CFData;

    use super::{copy_cf_data, keychain_error, ERR_SEC_MISSING_ENTITLEMENT, ERR_SEC_USER_CANCELED};
    use crate::error::ErrorCode;

    #[test]
    fn oversized_native_value_is_rejected_before_copying_bytes() {
        let data = CFData::from_buffer(b"protected-value");
        let error = copy_cf_data(data.as_CFTypeRef(), 4).unwrap_err();
        assert_eq!(error.code, ErrorCode::ResponseTooLarge);
    }

    #[test]
    fn bounded_native_value_is_copied_into_zeroizing_storage() {
        let data = CFData::from_buffer(b"value");
        let copied = copy_cf_data(data.as_CFTypeRef(), 5).unwrap();
        assert_eq!(copied.as_slice(), b"value");
    }

    #[test]
    fn native_statuses_map_to_fixed_public_codes() {
        assert_eq!(
            keychain_error(ERR_SEC_MISSING_ENTITLEMENT, ErrorCode::SessionNotFound).code,
            ErrorCode::HelperSigningInvalid
        );
        assert_eq!(
            keychain_error(ERR_SEC_USER_CANCELED, ErrorCode::SessionNotFound).code,
            ErrorCode::UserPresenceCancelled
        );
        assert_eq!(
            keychain_error(-999_999, ErrorCode::SessionNotFound).code,
            ErrorCode::InternalFailure
        );
    }
}
