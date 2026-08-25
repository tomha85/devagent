# Java and .NET support in v0.7

Java discovery recognizes Maven, Maven Wrapper, Gradle, Gradle Kotlin DSL, and related settings manifests. .NET discovery recognizes solution files plus C#, F#, and Visual Basic project files. Test-project detection uses explicit project metadata rather than assuming every .NET project contains runnable tests.

Production Qualification includes real Maven/JUnit execution and a real .NET build, so v0.7 support is not based only on filename recognition.
