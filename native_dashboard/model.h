#pragma once
#include <QByteArray>
#include <QString>

// Expand extensions unsupported by the Qt 6.2 glTF importer into core glTF.
QByteArray compatibleGlb(const QByteArray &source, QString &error);
