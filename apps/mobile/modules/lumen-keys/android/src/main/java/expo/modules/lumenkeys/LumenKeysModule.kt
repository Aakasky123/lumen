package expo.modules.lumenkeys

import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyPermanentlyInvalidatedException
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import android.util.Base64
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_STRONG
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import expo.modules.kotlin.Promise
import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.functions.Queues
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/**
 * The phone's step-up key: an EC P-256 key in the Android Keystore that signs only after a strong
 * biometric check, every single use (no validity window). Lumen's backend holds the public half from
 * pairing and verifies these signatures before payments, vault writes, and setting changes.
 */
class LumenKeysModule : Module() {
  private val keyStore: KeyStore
    get() = KeyStore.getInstance(KEYSTORE).apply { load(null) }

  override fun definition() = ModuleDefinition {
    Name("LumenKeys")

    Function("biometricStatus") {
      val context = appContext.reactContext ?: return@Function "unavailable"
      when (BiometricManager.from(context).canAuthenticate(BIOMETRIC_STRONG)) {
        BiometricManager.BIOMETRIC_SUCCESS -> "ready"
        BiometricManager.BIOMETRIC_ERROR_NONE_ENROLLED -> "not_enrolled"
        else -> "unavailable"
      }
    }

    Function("hasKey") { alias: String ->
      keyStore.containsAlias(alias)
    }

    Function("deleteKey") { alias: String ->
      val store = keyStore
      if (store.containsAlias(alias)) store.deleteEntry(alias)
    }

    // Returns the public key as base64 SubjectPublicKeyInfo DER, the format pairing expects.
    AsyncFunction("createKey") { alias: String ->
      val store = keyStore
      if (store.containsAlias(alias)) store.deleteEntry(alias)
      val publicKey = try {
        generate(alias, strongBox = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P)
      } catch (error: StrongBoxUnavailableException) {
        generate(alias, strongBox = false)
      }
      Base64.encodeToString(publicKey, Base64.NO_WRAP)
    }

    // Shows the system biometric prompt bound to this one signature; resolves base64 DER.
    AsyncFunction("sign") { alias: String, payload: String, title: String, subtitle: String, promise: Promise ->
      val activity = appContext.currentActivity as? FragmentActivity
      if (activity == null) {
        promise.reject(CodedException("NO_ACTIVITY", "Lumen isn't in the foreground.", null))
        return@AsyncFunction
      }
      val signature = try {
        val key = keyStore.getKey(alias, null) as? PrivateKey
          ?: throw CodedException("NO_KEY", "This phone has no Lumen key. Pair it again.", null)
        Signature.getInstance("SHA256withECDSA").apply { initSign(key) }
      } catch (error: KeyPermanentlyInvalidatedException) {
        promise.reject(CodedException("KEY_INVALIDATED", "Your fingerprints changed, so this phone must be paired again.", error))
        return@AsyncFunction
      } catch (error: CodedException) {
        promise.reject(error)
        return@AsyncFunction
      } catch (error: Exception) {
        promise.reject(CodedException("SIGN_SETUP_FAILED", error.message ?: "Couldn't prepare the signature.", error))
        return@AsyncFunction
      }

      val callback = object : BiometricPrompt.AuthenticationCallback() {
        override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
          try {
            val unlocked = result.cryptoObject?.signature
              ?: throw IllegalStateException("The biometric check returned no signature.")
            unlocked.update(payload.toByteArray(Charsets.UTF_8))
            promise.resolve(Base64.encodeToString(unlocked.sign(), Base64.NO_WRAP))
          } catch (error: Exception) {
            promise.reject(CodedException("SIGN_FAILED", error.message ?: "Couldn't sign.", error))
          }
        }

        override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
          val cancelled = errorCode == BiometricPrompt.ERROR_USER_CANCELED ||
            errorCode == BiometricPrompt.ERROR_NEGATIVE_BUTTON ||
            errorCode == BiometricPrompt.ERROR_CANCELED
          promise.reject(CodedException(if (cancelled) "CANCELLED" else "AUTH_ERROR", errString.toString(), null))
        }
      }

      val prompt = BiometricPrompt(activity, ContextCompat.getMainExecutor(activity), callback)
      val info = BiometricPrompt.PromptInfo.Builder()
        .setTitle(title)
        .setSubtitle(subtitle)
        .setNegativeButtonText("Cancel")
        .setAllowedAuthenticators(BIOMETRIC_STRONG)
        .setConfirmationRequired(true)
        .build()
      prompt.authenticate(info, BiometricPrompt.CryptoObject(signature))
    }.runOnQueue(Queues.MAIN)
  }

  private fun generate(alias: String, strongBox: Boolean): ByteArray {
    val builder = KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
      .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
      .setDigests(KeyProperties.DIGEST_SHA256)
      .setUserAuthenticationRequired(true)
      .setInvalidatedByBiometricEnrollment(true)
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
      // Timeout 0: every signature needs its own strong biometric check.
      builder.setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)
    } else {
      @Suppress("DEPRECATION")
      builder.setUserAuthenticationValidityDurationSeconds(-1)
    }
    if (strongBox && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
      builder.setIsStrongBoxBacked(true)
    }
    val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, KEYSTORE)
    generator.initialize(builder.build())
    return generator.generateKeyPair().public.encoded
  }

  companion object {
    private const val KEYSTORE = "AndroidKeyStore"
  }
}
