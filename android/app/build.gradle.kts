plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// Signature de publication : fournie par la CI (secrets GitHub), jamais dans le dépôt en clair
// (inv. 5). Sans elle (poste de développement), la clé de débogage signe l'APK.
val releaseKeystore: String? = System.getenv("FINANCES_KEYSTORE")

android {
    namespace = "perso.finances"
    compileSdk = 35

    defaultConfig {
        applicationId = "perso.finances"
        minSdk = 29
        targetSdk = 35
        versionCode = (System.getenv("FINANCES_VERSION_CODE") ?: "1").toInt()
        versionName = System.getenv("FINANCES_VERSION_NAME") ?: "0.1-dev"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    // Réponses de référence de l'API partagées entre tests JVM et tests sur appareil.
    sourceSets {
        getByName("test") {
            kotlin.directories += "src/sharedTest/java"
            resources.directories += "src/sharedTest/resources"
        }
        getByName("androidTest") {
            kotlin.directories += "src/sharedTest/java"
            resources.directories += "src/sharedTest/resources"
        }
    }

    signingConfigs {
        if (releaseKeystore != null) {
            create("release") {
                storeFile = file(releaseKeystore)
                storePassword = System.getenv("FINANCES_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("FINANCES_KEY_ALIAS") ?: "finances"
                keyPassword = System.getenv("FINANCES_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            // En CI : APK non signé, signé ensuite par apksigner dans un job sans code du dépôt
            // (jamais de repli silencieux sur la clé de débogage). En local : clé de débogage.
            signingConfig = signingConfigs.findByName("release")
                ?: if (System.getenv("CI") == null) signingConfigs.getByName("debug") else null
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
        compose = true
    }
    testOptions {
        unitTests.isIncludeAndroidResources = true
        // Message et trace complets d'un test en échec dans le journal de la CI.
        unitTests.all {
            it.testLogging {
                events("failed")
                exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
            }
        }
    }
    lint {
        abortOnError = true
        warningsAsErrors = true
        // Contrôles qui dépendent de la date ou du SDK installé (construction non reproductible) :
        // les montées de version se font volontairement (Renovate).
        disable += setOf("GradleDependency", "NewerVersionAvailable", "AndroidGradlePluginVersion", "OldTargetApi")
    }
}

// Kotlin intégré à AGP 9 : la cible JVM suit compileOptions (17).
kotlin {
    compilerOptions {
        allWarningsAsErrors = true
    }
}

dependencies {
    implementation(libs.androidx.core)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime)
    implementation(libs.androidx.lifecycle.viewmodel)
    implementation(libs.androidx.navigation.compose)
    implementation(libs.androidx.biometric)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.material3)
    implementation(libs.compose.material.icons)
    implementation(libs.okhttp)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)

    testImplementation(libs.junit)
    testImplementation(libs.okhttp.mockwebserver)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.ext.junit)
    testImplementation(platform(libs.compose.bom))
    testImplementation(libs.compose.ui.test.junit4)
    debugImplementation(libs.compose.ui.test.manifest)

    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.okhttp.mockwebserver)
    androidTestImplementation(platform(libs.compose.bom))
    androidTestImplementation(libs.compose.ui.test.junit4)
}
