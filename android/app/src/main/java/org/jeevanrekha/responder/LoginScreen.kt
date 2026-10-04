package org.jeevanrekha.responder

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import java.io.IOException

@Composable
fun LoginScreen(onLoggedIn: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    var server by remember { mutableStateOf(Session.serverUrl(context)) }
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("JeevanRekha", fontSize = 30.sp, fontWeight = FontWeight.Bold,
            color = MaterialTheme.colorScheme.error)
        Text("Responder sign in", fontSize = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(24.dp))

        OutlinedTextField(
            value = server,
            onValueChange = { server = it },
            label = { Text("Server URL") },
            singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = username,
            onValueChange = { username = it },
            label = { Text("Username") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(
            value = password,
            onValueChange = { password = it },
            label = { Text("Password") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
            modifier = Modifier.fillMaxWidth(),
        )

        error?.let {
            Spacer(Modifier.height(12.dp))
            Text(it, color = MaterialTheme.colorScheme.error, fontSize = 14.sp)
        }

        Spacer(Modifier.height(20.dp))
        Button(
            onClick = {
                busy = true
                error = null
                scope.launch {
                    val result = runCatching {
                        Api.login(context, server, username.trim(), password)
                    }
                    busy = false
                    result.onSuccess {
                        // Best-effort: registers this device for emergency
                        // push. Without a real Firebase project it fails
                        // quietly and the app keeps working via polling.
                        Push.register(context)
                        onLoggedIn()
                    }.onFailure { e ->
                        error = when (e) {
                            is Api.ApiException ->
                                if (e.code == 401) "Wrong username or password"
                                else "Server error (${e.code})"
                            is IOException ->
                                "Cannot reach the server — check the URL and your connection"
                            else -> e.message ?: "Login failed"
                        }
                    }
                }
            },
            enabled = !busy && username.isNotBlank() && password.isNotBlank() &&
                server.isNotBlank(),
            modifier = Modifier.fillMaxWidth().height(56.dp),
        ) {
            Text(if (busy) "Signing in…" else "Sign in", fontSize = 16.sp)
        }

        Spacer(Modifier.height(16.dp))
        Text(
            "After sign-in this device registers for emergency push alerts. " +
                "Please allow notifications and keep battery optimisation OFF " +
                "for this app, or alerts may not wake the phone.",
            fontSize = 12.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
