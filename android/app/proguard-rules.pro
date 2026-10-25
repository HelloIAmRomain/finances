# kotlinx.serialization : conserver les sérialiseurs générés des modèles de l'API.
-keepattributes *Annotation*, InnerClasses
-keepclassmembers class perso.finances.api.** {
    *** Companion;
}
-keepclasseswithmembers class perso.finances.api.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-dontwarn org.bouncycastle.**
-dontwarn org.conscrypt.**
-dontwarn org.openjsse.**
